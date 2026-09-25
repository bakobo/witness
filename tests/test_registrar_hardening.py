"""Fail-opens Codex found in PR #19, each pinned by the test that would have caught it."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import falcon.testing
import fiki
import pytest

from witness.errors import (RegistrarCallbackRefused, RegistrarFull, RegistrarInstance)
from witness.registrar.app import make_registrar_app
from witness.registrar.batcher import Batcher, CallbackPolicy, HttpTransport
from witness.registrar.store import RegistrarStore

BASE = "http://falconframework.org"
SUBSCRIPTION = "/v1/registrar/subscription"


@pytest.fixture
def store(tmp_path):
    opened = RegistrarStore(tmp_path / "registrar.sqlite3")
    yield opened
    opened.close()


def _signed_request(key, method, path, document=None):
    body = None if document is None else json.dumps(document).encode()
    return fiki.sign_request(key=key, method=method, url=BASE + path, body=body), body


# --- (1) SSRF: callbacks to private destinations, and redirects --------------------------------

class _Sink(BaseHTTPRequestHandler):
    hits = []
    redirect_to = None
    delay = 0.0

    def log_message(self, *args):
        pass

    def do_POST(self):
        type(self).hits.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(type(self).delay)
        if type(self).redirect_to and self.path == "/batch":
            self.send_response(307)
            self.send_header("Location", type(self).redirect_to)
        else:
            self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def sink():
    _Sink.hits, _Sink.redirect_to, _Sink.delay = [], None, 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/b", "http://localhost:9/b",
                                 "http://169.254.169.254/latest", "http://10.0.0.5/b",
                                 "http://[::1]:9/b", "http://0.0.0.0:9/b"])
def test_private_destinations_are_refused_by_default(url):
    with pytest.raises(RegistrarCallbackRefused) as refused:
        CallbackPolicy().check(url)
    assert refused.value.code == "e.grant.scope.callback.f"


def test_an_allowlisted_cidr_or_host_is_permitted():
    assert CallbackPolicy(allow=("127.0.0.0/8",)).check("http://127.0.0.1:9/b") == "127.0.0.1"
    assert CallbackPolicy(allow=("localhost",)).check("http://localhost:9/b") in {"127.0.0.1",
                                                                                "::1"}


def test_an_unresolvable_callback_is_refused():
    with pytest.raises(RegistrarCallbackRefused):
        CallbackPolicy().check("http://no-such-host.invalid/b")


def test_a_bad_allow_entry_is_refused_at_configuration():
    with pytest.raises(ValueError):
        CallbackPolicy(allow=("not a cidr/99",))


def test_the_transport_refuses_a_loopback_callback_unless_allowlisted(sink):
    url = f"http://127.0.0.1:{sink}/batch"
    with pytest.raises(RegistrarCallbackRefused):
        HttpTransport(timeout=5).send(url, b"{}", {})
    assert _Sink.hits == []
    HttpTransport(timeout=5, policy=CallbackPolicy(allow=("127.0.0.0/8",))).send(url, b"{}", {})
    assert _Sink.hits == ["/batch"]


def test_the_transport_never_follows_a_redirect(sink):
    _Sink.redirect_to = f"http://127.0.0.1:{sink}/elsewhere"
    transport = HttpTransport(timeout=5, policy=CallbackPolicy(allow=("127.0.0.0/8",)))
    with pytest.raises(ConnectionError, match="307"):
        transport.send(f"http://127.0.0.1:{sink}/batch", b"{}", {})
    assert _Sink.hits == ["/batch"], "the redirect target was never contacted"


def test_subscribing_a_private_callback_is_refused_by_default(store):
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60))
    key = fiki.Key.generate()
    headers, body = _signed_request(key, "POST", SUBSCRIPTION,
                                    {"callback": "http://127.0.0.1:9/b"})
    answer = app.simulate_post(SUBSCRIPTION, headers=headers, body=body)
    assert (answer.status_code, answer.json["code"]) == (403, "e.grant.scope.callback.f")
    assert store.subscribers() == []


# --- (2) a second Registrar on the same store --------------------------------------------------

def test_a_second_registrar_on_the_same_store_is_refused(tmp_path):
    first = RegistrarStore(tmp_path / "registrar.sqlite3")
    try:
        with pytest.raises(RegistrarInstance) as busy:
            RegistrarStore(tmp_path / "registrar.sqlite3")
        assert busy.value.code == "e.state.conflict.registrar.instance.f"
    finally:
        first.close()
    RegistrarStore(tmp_path / "registrar.sqlite3").close()  # released on close


# --- (3) a subscription that vanishes mid-tick -------------------------------------------------

class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, url, body, headers):
        self.sent.append(url)


def test_a_subscription_that_disappears_mid_tick_does_not_stop_the_others(store):
    store.subscribe("EGone", "http://a.example/batch")
    store.subscribe("EStays", "http://b.example/batch")
    listed = store.subscribers()
    store.unsubscribe("EGone")
    store.subscribers = lambda: listed  # the list was taken before the unsubscribe
    transport = _Recorder()
    assert Batcher(store=store, transport=transport, window=5).tick() == 1
    assert transport.sent == ["http://b.example/batch"]


def test_the_batch_thread_survives_a_failing_tick(store, monkeypatch):
    batcher = Batcher(store=store, transport=_Recorder(), window=0.01)
    ticks = []

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("one bad window")
        return 0

    monkeypatch.setattr(batcher, "tick", tick)
    stop = threading.Event()
    thread = threading.Thread(target=batcher.run, args=(stop,))
    thread.start()
    while len(ticks) < 3:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert len(ticks) >= 3, "the thread outlived the failure"


# --- (4) replayed and repeated subscriptions ---------------------------------------------------

def test_resubscribing_with_the_same_callback_keeps_the_sequence(store):
    store.subscribe("EObs", "http://a.example/batch")
    store.compose("EObs")
    store.compose("EObs")
    store.subscribe("EObs", "http://a.example/batch")
    assert store.compose("EObs").number == 3


def test_a_changed_callback_continues_the_sequence(store):
    store.subscribe("EObs", "http://a.example/batch")
    store.compose("EObs")
    store.subscribe("EObs", "http://b.example/batch")
    batch = store.compose("EObs")
    assert (batch.number, batch.full) == (2, False)
    assert store.subscribers() == [("EObs", "http://b.example/batch")]


def test_a_resubscription_after_unsubscribing_starts_again_full(store):
    store.subscribe("EObs", "http://a.example/batch")
    store.compose("EObs")
    store.unsubscribe("EObs")
    store.subscribe("EObs", "http://a.example/batch")
    batch = store.compose("EObs")
    assert (batch.number, batch.full) == (1, True)


def test_a_replayed_signed_subscription_request_is_refused(store):
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60,
        callbacks=CallbackPolicy(allow=("a.example",), resolve=lambda host: ["203.0.113.5"])))
    key = fiki.Key.generate()
    headers, body = _signed_request(key, "POST", SUBSCRIPTION,
                                    {"callback": "http://a.example/batch"})
    assert app.simulate_post(SUBSCRIPTION, headers=headers, body=body).status_code == 200
    replay = app.simulate_post(SUBSCRIPTION, headers=headers, body=body)
    assert (replay.status_code, replay.json["code"]) == (409,
                                                         "e.state.conflict.registrar.replay.f")
    headers, _ = _signed_request(key, "DELETE", SUBSCRIPTION)
    assert app.simulate_delete(SUBSCRIPTION, headers=headers).status_code == 204
    again = app.simulate_delete(SUBSCRIPTION, headers=headers)
    assert again.json["code"] == "e.state.conflict.registrar.replay.f"


def test_seen_signatures_expire_with_their_freshness_window(store):
    assert store.first_sighting("EObs", 1000, "sig=:A:", now=1000, max_age=60) is True
    assert store.first_sighting("EObs", 1000, "sig=:A:", now=1030, max_age=60) is False
    assert store.first_sighting("EObs", 1000, "sig=:A:", now=1061, max_age=60) is True


# --- (5) caps and concurrent, bounded delivery -------------------------------------------------

def test_subscriptions_are_capped_in_total(store):
    store.subscribe("EOne", "http://a.example/1", limit=2)
    store.subscribe("ETwo", "http://a.example/2", limit=2)
    store.subscribe("EOne", "http://a.example/1b", limit=2)  # an existing AID is not new
    with pytest.raises(RegistrarFull) as full:
        store.subscribe("EThree", "http://a.example/3", limit=2)
    assert full.value.code == "e.grant.quota.subscriptions.r"


def test_a_slow_callback_does_not_delay_the_others(store, sink):
    _Sink.delay = 2.0
    slow_port = sink
    fast = ThreadingHTTPServer(("127.0.0.1", 0), type("Fast", (_Sink,), {"delay": 0.0,
                                                                         "hits": []}))
    threading.Thread(target=fast.serve_forever, daemon=True).start()
    try:
        store.subscribe("ESlow", f"http://127.0.0.1:{slow_port}/slow")
        store.subscribe("EFast", f"http://127.0.0.1:{fast.server_address[1]}/fast")
        transport = HttpTransport(timeout=0.3, policy=CallbackPolicy(allow=("127.0.0.0/8",)))
        started = time.monotonic()
        delivered = Batcher(store=store, transport=transport, window=5).tick()
        assert time.monotonic() - started < 1.5
        assert delivered == 1
        assert store.compose("ESlow").number == 2, "the failed delivery spent its number"
    finally:
        fast.shutdown()


def test_a_delivery_timeout_must_be_well_under_the_window(store):
    with pytest.raises(ValueError, match="timeout"):
        Batcher(store=store, transport=HttpTransport(timeout=5), window=5)


def test_a_quiet_registrar_with_no_subscribers_sends_nothing(store):
    transport = _Recorder()
    assert Batcher(store=store, transport=transport, window=5).tick() == 0
    assert transport.sent == []


def test_an_https_callback_is_wrapped_with_the_callback_host_for_sni(monkeypatch):
    from witness.registrar import batcher as module
    wrapped = {}

    class Context:
        def wrap_socket(self, sock, server_hostname):
            wrapped.update(sock=sock, host=server_hostname)
            return "tls-socket"

    monkeypatch.setattr(module.socket, "create_connection",
                        lambda address, timeout: ("plain-socket", address))
    monkeypatch.setattr(module.ssl, "create_default_context", Context)
    connection = module._PinnedHTTPS("observer.example", None, address="203.0.113.9", timeout=1)
    connection.connect()
    assert wrapped == {"sock": ("plain-socket", ("203.0.113.9", 443)),
                       "host": "observer.example"}
    assert connection.sock == "tls-socket"
