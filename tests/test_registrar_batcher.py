"""The batch clock: one signed batch per subscriber per window, empty when nothing changed."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fiki
import pytest

from witness.registrar.batcher import Batcher, CallbackPolicy, HttpTransport

LOOPBACK = CallbackPolicy(allow=("127.0.0.0/8",))
from witness.registrar.store import RegistrarStore


class Recorder:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, url, body, headers):
        if self.fail:
            raise ConnectionError("the Observer is down")
        self.sent.append((url, body, headers))


@pytest.fixture
def store(tmp_path):
    opened = RegistrarStore(tmp_path / "registrar.sqlite3")
    yield opened
    opened.close()


def _batch(sent):
    return json.loads(sent[1])


def test_each_window_sends_one_signed_batch_to_each_subscriber(store):
    store.publish(registry="r1", issuer="EIssuer", digest="d1", chain=b"rip", kel=b"kel")
    store.subscribe("EObserverA", "http://127.0.0.1:1/a", "nonce-of-observer-a0")
    store.subscribe("EObserverB", "http://127.0.0.1:1/b", "nonce-of-observer-b0")
    transport = Recorder()
    batcher = Batcher(store=store, transport=transport, window=5, clock=lambda: 1000.0)
    assert batcher.tick() == 2
    assert sorted(url for url, _, _ in transport.sent) == ["http://127.0.0.1:1/a",
                                                           "http://127.0.0.1:1/b"]
    url, body, headers = transport.sent[0]
    verdict = fiki.verify_request(method="POST", url=url, headers=headers, body=body,
                                  max_age=None, expected_aid=store.key().aid)
    assert "content-digest" in verdict.covered
    batch = json.loads(body)
    assert batch["registrar"] == store.key().aid
    assert sorted(json.loads(sent)["subscription"] for _, sent, _ in transport.sent) == \
        ["nonce-of-observer-a0", "nonce-of-observer-b0"], \
        "each batch names, under the signature, the subscription it was sent for"
    assert (batch["number"], batch["full"], batch["window_end"]) == \
        (1, True, "1970-01-01T00:16:40+00:00")
    head, = batch["heads"]
    assert base64.b64decode(head["chain"]) == b"rip"
    assert set(head) == {"registry", "issuer", "digest", "chain", "kel"}


def test_a_quiet_window_still_sends_an_empty_batch(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000")
    transport = Recorder()
    batcher = Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0)
    batcher.tick()
    batcher.tick()
    assert [(_batch(sent)["number"], _batch(sent)["heads"]) for sent in transport.sent] == \
        [(1, []), (2, [])]


def test_a_failed_delivery_consumes_its_number_so_the_observer_sees_a_gap(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000")
    batcher = Batcher(store=store, transport=Recorder(fail=True), window=5, clock=lambda: 0.0)
    assert batcher.tick() == 0
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    assert _batch(transport.sent[0])["number"] == 2


def test_the_loop_ticks_every_window_until_stopped(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000")
    transport = Recorder()
    batcher = Batcher(store=store, transport=transport, window=0.01, clock=lambda: 0.0)
    stop = threading.Event()
    thread = threading.Thread(target=batcher.run, args=(stop,))
    thread.start()
    while len(transport.sent) < 3:
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_window_must_be_positive(store):
    with pytest.raises(ValueError, match="window"):
        Batcher(store=store, transport=Recorder(), window=0)


class _Sink(BaseHTTPRequestHandler):
    received = []
    status = 204

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def sink():
    _Sink.received, _Sink.status = [], 204
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/batch"
    server.shutdown()


def test_the_http_transport_posts_the_body_and_headers(sink):
    HttpTransport(timeout=5, policy=LOOPBACK).send(sink, b'{"number": 1}',
                                                   {"Signature": "sig=:AA:"})
    headers, body = _Sink.received[0]
    assert body == b'{"number": 1}'
    assert headers["Signature"] == "sig=:AA:"
    assert headers["Content-Type"] == "application/json"


def test_the_http_transport_raises_when_the_observer_refuses(sink):
    _Sink.status = 409
    with pytest.raises(ConnectionError, match="409"):
        HttpTransport(timeout=5, policy=LOOPBACK).send(sink, b"{}", {})
