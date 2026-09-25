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
        ticks.append("failed" if not ticks else "ran")
        if len(ticks) == 1:
            raise RuntimeError("one bad window")
        return len(ticks)

    monkeypatch.setattr(batcher, "tick", tick)
    stop = threading.Event()
    thread = threading.Thread(target=batcher.run, args=(stop,))
    thread.start()
    while len(ticks) < 3:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert ticks[0] == "failed" and ticks[1:3] == ["ran", "ran"], \
        "the windows after a failing one still ran"


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


def test_a_sighting_is_kept_for_the_verifiers_whole_acceptance_window(store):
    """fiki accepts a request until created + max_age + skew, so the sighting must outlive that."""
    from witness.registrar.app import SKEW
    keep = 60 + SKEW
    assert store.first_sighting("EObs", 1000, b"sig", now=1000, keep=keep) is True
    assert store.first_sighting("EObs", 1000, b"sig", now=1000 + 60 + SKEW, keep=keep) is False
    assert store.first_sighting("EObs", 1000, b"sig", now=1001 + 60 + SKEW, keep=keep) is True


def test_expired_sightings_are_pruned_on_every_insert(store):
    store.first_sighting("EOld", 1000, b"old", now=1000, keep=10)
    store.first_sighting("ENew", 2000, b"new", now=2000, keep=10)
    assert store.sighting_count() == 1


def test_a_relabelled_replay_is_refused(store):
    """The label in Signature: sig=:...: is not signed, so relabelling a captured request must not
    give it a new identity. Key on the signature bytes, not the header text."""
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60,
        callbacks=CallbackPolicy(allow=("a.example",), resolve=lambda host: ["203.0.113.5"])))
    key = fiki.Key.generate()
    headers, body = _signed_request(key, "POST", SUBSCRIPTION,
                                    {"callback": "http://a.example/batch"})
    assert app.simulate_post(SUBSCRIPTION, headers=headers, body=body).status_code == 200
    delete, _ = _signed_request(key, "DELETE", SUBSCRIPTION)
    assert app.simulate_delete(SUBSCRIPTION, headers=delete).status_code == 204
    fresh, body = _signed_request(key, "POST", SUBSCRIPTION,
                                  {"callback": "http://a.example/other"})
    assert app.simulate_post(SUBSCRIPTION, headers=fresh, body=body).status_code == 200
    relabelled = {name: value.replace("sig=", "other=", 1)
                  if name.lower() in ("signature", "signature-input") else value
                  for name, value in delete.items()}
    fiki.verify_request(method="DELETE", url=BASE + SUBSCRIPTION, headers=relabelled,
                        max_age=60)  # fiki itself accepts the relabelled request
    again = app.simulate_delete(SUBSCRIPTION, headers=relabelled)
    assert (again.status_code, again.json["code"]) == (409,
                                                       "e.state.conflict.registrar.replay.f")
    assert store.subscribers() != [], "the captured DELETE did not end the new subscription"


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
        minimum_version = None

        def wrap_socket(self, sock, server_hostname):
            wrapped.update(sock=sock, host=server_hostname, floor=self.minimum_version)
            return "tls-socket"

    monkeypatch.setattr(module.socket, "create_connection",
                        lambda address, timeout: ("plain-socket", address))
    monkeypatch.setattr(module.ssl, "create_default_context", Context)
    connection = module._PinnedHTTPS("observer.example", None, address="203.0.113.9", timeout=1)
    connection.connect()
    assert wrapped == {"sock": ("plain-socket", ("203.0.113.9", 443)),
                       "host": "observer.example", "floor": module.ssl.TLSVersion.TLSv1_2}
    assert connection.sock == "tls-socket"


# --- (3 again) one wall-clock deadline on the whole delivery -----------------------------------

class _Trickle(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            for byte in b"HTTP/1.1 200 OK\r\n" * 100:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:  # the Registrar abandoned the delivery and shut the socket
            return


def test_a_trickling_callback_is_abandoned_at_the_deadline():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        transport = HttpTransport(timeout=0.5, policy=CallbackPolicy(allow=("127.0.0.0/8",)))
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.send(f"http://127.0.0.1:{server.server_address[1]}/batch", b"{}", {})
        assert time.monotonic() - started < 1.5
    finally:
        server.shutdown()


def test_slow_name_resolution_counts_against_the_deadline():
    def slow(host):
        time.sleep(2)
        return ["203.0.113.7"]

    transport = HttpTransport(timeout=0.3, policy=CallbackPolicy(allow=(), resolve=slow))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        transport.send("http://slow.example/batch", b"{}", {})
    assert time.monotonic() - started < 1.0


def test_an_abandoned_delivery_still_spends_its_number(store):
    store.subscribe("ESlow", "http://slow.example/batch")

    class Stalled:
        timeout = 0.2

        def send(self, url, body, headers):
            raise TimeoutError("abandoned at the deadline")

    assert Batcher(store=store, transport=Stalled(), window=5).tick() == 0
    assert store.compose("ESlow").number == 2


# --- (4 again) the lock follows the database, not the directory name ---------------------------

def test_a_symlinked_database_is_the_same_store(tmp_path):
    real = tmp_path / "real"
    other = tmp_path / "other"
    real.mkdir()
    other.mkdir()
    first = RegistrarStore(real / "registrar.sqlite3")
    try:
        (other / "registrar.sqlite3").symlink_to(real / "registrar.sqlite3")
        with pytest.raises(RegistrarInstance):
            RegistrarStore(other / "registrar.sqlite3")
    finally:
        first.close()


def test_abandoning_a_connection_with_no_socket_or_a_closed_one_is_harmless():
    from types import SimpleNamespace
    from witness.registrar import batcher as module

    class Closed:
        def shutdown(self, how):
            raise OSError("already closed")

    module._shut(SimpleNamespace(sock=None))
    module._shut(SimpleNamespace(sock=Closed()))


# --- round 3: one instant for the verifier and the pruner --------------------------------------

def test_a_replay_at_the_edge_of_the_window_is_refused_even_as_the_clock_ticks(store):
    """The verifier and the pruner must read the same instant. With two reads, a replay verified
    at created + max_age + SKEW could find its sighting pruned by a clock that has just ticked."""
    from witness.registrar.app import SKEW
    created = 1_000_000
    edge = created + 60 + SKEW
    ticks = iter([created, edge, edge + 1, edge + 1])
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60, clock=lambda: next(ticks),
        callbacks=CallbackPolicy(allow=("a.example",), resolve=lambda host: ["203.0.113.5"])))
    key = fiki.Key.generate()
    body = json.dumps({"callback": "http://a.example/batch"}).encode()
    headers = fiki.sign_request(key=key, method="POST", url=BASE + SUBSCRIPTION, body=body,
                                created=created)
    assert app.simulate_post(SUBSCRIPTION, headers=headers, body=body).status_code == 200
    replay = app.simulate_post(SUBSCRIPTION, headers=headers, body=body)
    assert (replay.status_code, replay.json["code"]) == (409,
                                                         "e.state.conflict.registrar.replay.f")


# --- round 3: stuck resolvers, in-flight caps, and the connect race ----------------------------

class _Stuck:
    """A resolver that never returns until released, counting how often it was asked."""

    def __init__(self):
        self.calls, self.release = 0, threading.Event()

    def __call__(self, host):
        self.calls += 1
        self.release.wait(30)
        return ["203.0.113.8"]


def test_a_stuck_subscriber_gets_no_new_delivery_while_one_is_pending(store):
    stuck = _Stuck()
    store.subscribe("EStuck", "http://stuck.example/batch")
    transport = HttpTransport(timeout=0.05, policy=CallbackPolicy(resolve=stuck))
    batcher = Batcher(store=store, transport=transport, window=5)
    try:
        for _ in range(5):
            assert batcher.tick() == 0
        assert stuck.calls == 1, "no new resolver thread while the first is still stuck"
        assert transport.in_flight() == 1
        assert store.compose("EStuck").number == 6, "every window's number was spent"
    finally:
        stuck.release.set()


def test_in_flight_deliveries_are_capped_globally():
    stuck = _Stuck()
    transport = HttpTransport(timeout=0.05, policy=CallbackPolicy(resolve=stuck),
                              max_in_flight=2)
    try:
        for index in range(2):
            with pytest.raises(TimeoutError):
                transport.send(f"http://stuck{index}.example/batch", b"{}", {})
        with pytest.raises(ConnectionError, match="in flight"):
            transport.send("http://stuck9.example/batch", b"{}", {})
        assert stuck.calls == 2
    finally:
        stuck.release.set()
    for _ in range(100):
        if transport.in_flight() == 0:
            break
        time.sleep(0.01)
    assert transport.in_flight() == 0, "released work is no longer counted"


def test_a_socket_acquired_after_the_deadline_is_closed(monkeypatch):
    from witness.registrar import batcher as module
    closed = []

    class Late:
        def close(self):
            closed.append(True)

    def slow_connect(address, timeout):
        time.sleep(0.05)
        return Late()

    monkeypatch.setattr(module.socket, "create_connection", slow_connect)
    connection = module._PinnedHTTP("late.example", 80, address="203.0.113.9", timeout=1,
                                    deadline=time.monotonic() + 0.01)
    with pytest.raises(TimeoutError):
        connection.connect()
    assert closed == [True]
    assert connection.sock is None


# --- Copilot round 2 -----------------------------------------------------------------------------

def _app(store, **extra):
    return falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60,
        callbacks=CallbackPolicy(allow=("a.example",), resolve=lambda host: ["203.0.113.5"]),
        **extra))


def _subscribe(app, key, callback="http://a.example/batch", created=None):
    body = json.dumps({"callback": callback}).encode()
    headers = fiki.sign_request(key=key, method="POST", url=BASE + SUBSCRIPTION, body=body,
                                created=created)
    return app.simulate_post(SUBSCRIPTION, headers=headers, body=body), headers, body


def test_a_refused_subscription_leaves_no_sighting_so_its_retry_is_not_a_replay(store):
    app = _app(store, max_subscriptions=1)
    _subscribe(app, fiki.Key.generate())
    refused, headers, body = _subscribe(app, fiki.Key.generate())
    assert refused.json["code"] == "e.grant.quota.subscriptions.r"
    assert refused.headers["Retry-After"], "a retryable refusal says when to retry"
    store.unsubscribe(store.subscribers()[0][0])
    retried = app.simulate_post(SUBSCRIPTION, headers=headers, body=body)
    assert retried.status_code == 200, "the same signed request succeeds once there is room"


def test_recent_signed_requests_are_bounded_per_signer_and_in_total(store):
    app = _app(store, max_sightings_per_signer=2, max_sightings=3)
    key = fiki.Key.generate()
    now = int(time.time())
    assert _subscribe(app, key, created=now)[0].status_code == 200
    assert _subscribe(app, key, created=now - 1)[0].status_code == 200
    third = _subscribe(app, key, created=now - 2)[0]
    assert (third.status_code, third.json["code"]) == (429, "e.grant.quota.requests.r")
    other = fiki.Key.generate()
    assert _subscribe(app, other, created=now)[0].status_code == 200
    fifth = _subscribe(app, fiki.Key.generate(), created=now)[0]
    assert fifth.json["code"] == "e.grant.quota.requests.r"
    assert store.sighting_count() == 3


def test_an_empty_chain_or_kel_is_not_a_publication(store):
    publisher = fiki.Key.generate()
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset({publisher.aid}), max_age=60))
    for field in ("chain", "kel"):
        document = {"registry": "EReg", "issuer": "EIss", "digest": "d",
                    "chain": "Y2hhaW4=", "kel": "a2Vs", field: ""}
        body = json.dumps(document).encode()
        headers = fiki.sign_request(key=publisher, method="POST",
                                    url=BASE + "/v1/registrar/publication", body=body)
        answer = app.simulate_post("/v1/registrar/publication", headers=headers, body=body)
        assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")
    assert store.head("EReg") is None


def test_a_callback_with_an_impossible_port_is_refused_at_subscription(store):
    answer, _, _ = _subscribe(_app(store), fiki.Key.generate(), "http://a.example:99999/batch")
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")
    assert store.subscribers() == []


def test_admission_resolution_is_bounded(store):
    stuck = _Stuck()
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60, admission_timeout=0.1,
        callbacks=CallbackPolicy(resolve=stuck)))
    try:
        started = time.monotonic()
        answer, _, _ = _subscribe(app, fiki.Key.generate(), "http://stuck.example/batch")
        assert time.monotonic() - started < 1.0
        assert (answer.status_code, answer.json["code"]) == (503,
                                                             "e.env.resolver.timeout.r")
        assert store.subscribers() == []
    finally:
        stuck.release.set()


def test_admission_resolvers_in_flight_are_capped(store, monkeypatch):
    from witness.registrar import app as module
    monkeypatch.setattr(module, "MAX_ADMISSIONS", 1)
    stuck = _Stuck()
    app = falcon.testing.TestClient(make_registrar_app(
        store, publishers=frozenset(), max_age=60, admission_timeout=0.05,
        callbacks=CallbackPolicy(resolve=stuck)))
    try:
        _subscribe(app, fiki.Key.generate(), "http://stuck.example/one")
        second, _, _ = _subscribe(app, fiki.Key.generate(), "http://stuck.example/two")
        assert second.json["code"] == "e.env.resolver.timeout.r"
        assert stuck.calls == 1, "no second resolver while the first is still stuck"
    finally:
        stuck.release.set()


def test_concurrent_sends_to_one_callback_start_one_delivery():
    stuck = _Stuck()
    transport = HttpTransport(timeout=0.2, policy=CallbackPolicy(resolve=stuck))
    outcomes = []

    def send():
        try:
            transport.send("http://stuck.example/batch", b"{}", {})
        except (TimeoutError, ConnectionError) as failure:
            outcomes.append(type(failure).__name__)

    threads = [threading.Thread(target=send) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert stuck.calls == 1
        assert outcomes.count("TimeoutError") == 1
        assert outcomes.count("ConnectionError") == 7
    finally:
        stuck.release.set()


def test_the_query_string_is_delivered_as_subscribed(sink):
    transport = HttpTransport(timeout=5, policy=CallbackPolicy(allow=("127.0.0.0/8",)))
    transport.send(f"http://127.0.0.1:{sink}/batch?observer=bar", b"{}", {})
    assert _Sink.hits == ["/batch?observer=bar"]
