"""A subscriber that declares a page size gets every batch larger than it in signed pages under one
number (@9665wt3d).

Measured on keri-bench's 40k fill: a full batch runs about 900 bytes a head and heti's Observer
reads at most 16 MiB, so past about 18,600 registries every full batch, the only way an Observer
recovers from a gap, was closed unread.
"""

import base64
import json
import sqlite3

import fiki
import pytest

from witness.registrar import batcher as batching
from witness.registrar.batcher import Batcher, HttpTransport
from witness.registrar.store import Batch, RegistrarStore

from test_registrar_app import NONCE, SUBSCRIPTION, _signed, client, publisher  # noqa: F401
from test_registrar_batcher import LOOPBACK, _Sink, sink  # noqa: F401


class Recorder:
    def __init__(self, fail_at=None):
        self.sent, self.fail_at = [], fail_at

    def send(self, url, body, headers):
        if self.fail_at is not None and len(self.sent) + 1 == self.fail_at:
            raise ConnectionError("the Observer stopped reading")
        self.sent.append((url, body, headers))


@pytest.fixture
def store(tmp_path):
    opened = RegistrarStore(tmp_path / "registrar.sqlite3")
    yield opened
    opened.close()


def _fill(store, count, size=200):
    for n in range(count):
        store.publish(registry=f"E{n:05d}", issuer="EIssuer", digest=f"d{n}",
                      chain=bytes([n % 256]) * size)


def _verify(store, url, body, headers):
    fiki.verify_request(method="POST", url=url, headers=headers, body=body, max_age=None,
                        expected_aid=store.key().aid, authorities=None)
    return json.loads(body)


# --- the store keeps what a subscriber declared ---


def test_a_declared_page_size_is_kept_with_the_subscription_and_a_repeat_may_change_it(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    assert store.compose("EObserver").pages == 65536
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=131072)
    assert store.compose("EObserver").pages == 131072
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000")
    assert store.compose("EObserver").pages is None, "a subscription without it declares none"


def test_a_store_from_before_pages_opens_and_serves_its_subscriptions_unpaged(tmp_path):
    path = tmp_path / "registrar.sqlite3"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE subscribers (aid TEXT PRIMARY KEY, callback TEXT NOT NULL, "
                    "number INTEGER NOT NULL, seen INTEGER NOT NULL, full INTEGER NOT NULL, "
                    "nonce TEXT NOT NULL)")
        old.execute("INSERT INTO subscribers VALUES ('EObserver', 'http://127.0.0.1:1/a', 0, 0, "
                    "1, 'nonce-00000000000000')")
    store = RegistrarStore(path)
    try:
        assert store.compose("EObserver").pages is None
    finally:
        store.close()


# --- the batcher pages what is larger than the declared size ---


def test_a_full_batch_past_the_declared_size_arrives_as_signed_pages_under_one_number(store):
    """THE PAGES. Each page verifies on its own, all carry the one number and `pages`, and
    together they hold exactly the snapshot's heads in registry order, each page within the
    declared size."""
    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    assert Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick() == 1

    pages = [_verify(store, *sent) for sent in transport.sent]
    assert len(pages) > 1
    assert all(len(body) <= 65536 for _, body, _ in transport.sent)
    assert [page["page"] for page in pages] == list(range(1, len(pages) + 1))
    assert {(page["number"], page["full"], page["pages"], page["subscription"])
            for page in pages} == {(1, True, len(pages), "nonce-00000000000000")}
    heads = [head["registry"] for page in pages for head in page["heads"]]
    assert heads == [f"E{n:05d}" for n in range(300)]


def test_a_batch_that_fits_is_sent_as_one_body_without_page_fields(store):
    _fill(store, 3)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    batch, = [_verify(store, *sent) for sent in transport.sent]
    assert "page" not in batch and "pages" not in batch
    assert len(batch["heads"]) == 3


def test_a_subscriber_that_declared_nothing_gets_one_body_whatever_its_size(store):
    """DEPLOY ORDER. An Observer that predates pages ignores fields it does not know, so it is
    never sent a page: it would apply page 1 as the whole snapshot."""
    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000")
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    batch, = [_verify(store, *sent) for sent in transport.sent]
    assert "page" not in batch and len(batch["heads"]) == 300


def test_the_registrars_own_ceiling_bounds_a_page_a_subscriber_declared_larger(
        store, monkeypatch):
    monkeypatch.setattr(batching, "PAGE_CEILING", 32768)
    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=10**8)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    assert len(transport.sent) > 1
    assert all(len(body) <= 32768 for _, body, _ in transport.sent)


def test_a_delta_larger_than_the_declared_size_is_paged_too(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    batcher = Batcher(store=store, transport=Recorder(), window=5, clock=lambda: 0.0)
    batcher.tick()  # the full batch 1, empty
    _fill(store, 300)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    pages = [_verify(store, *sent) for sent in transport.sent]
    assert len(pages) > 1 and {(page["number"], page["full"]) for page in pages} == {(2, False)}


def test_a_head_larger_than_a_page_travels_alone_on_its_own_page(store):
    """A page holds at least one head, so one larger than the page size is sent alone rather than
    dropped; whether the subscriber can read it is the subscriber's bound, as before pages."""
    store.publish(registry="E00000", issuer="EIssuer", digest="d0", chain=b"x" * 100000)
    _fill_after = [("E00001", b"y" * 100)]
    for registry, chain in _fill_after:
        store.publish(registry=registry, issuer="EIssuer", digest="d1", chain=chain)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    pages = [_verify(store, *sent) for sent in transport.sent]
    assert [[head["registry"] for head in page["heads"]] for page in pages] == \
        [["E00000"], ["E00001"]]


def test_a_failed_page_ends_the_delivery_and_spends_the_number(store):
    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder(fail_at=2)
    assert Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick() == 0
    assert len(transport.sent) == 1, "nothing after the failed page was sent"
    after = Recorder()
    Batcher(store=store, transport=after, window=5, clock=lambda: 0.0).tick()
    assert json.loads(after.sent[0][1])["number"] == 2


def test_a_page_split_of_an_empty_batch_is_the_one_empty_body():
    batch = Batch(number=1, full=True, heads=(), subscription="nonce-00000000000000",
                           pages=65536)
    body, = batching.encode_pages(batch, registrar="EReg", window_end=0.0)
    assert json.loads(body)["heads"] == []


# --- the subscription door ---


def test_a_subscription_may_declare_a_page_size(client):  # noqa: F811
    app, store = client
    observer = fiki.Key.generate()
    answer = _signed(app, observer, "POST", SUBSCRIPTION,
                     {"callback": "http://127.0.0.1:9000/batch", "nonce": NONCE,
                      "pages": 16 * 1024 * 1024})
    assert answer.status_code == 200
    assert store.compose(observer.aid).pages == 16 * 1024 * 1024


@pytest.mark.parametrize("pages", [0, -1, True, "16777216", 1.5, 1023, None])
def test_a_declared_page_size_is_a_whole_number_of_bytes_no_smaller_than_the_floor(
        client, pages):  # noqa: F811
    app, store = client
    answer = _signed(app, fiki.Key.generate(), "POST", SUBSCRIPTION,
                     {"callback": "http://127.0.0.1:9000/batch", "nonce": NONCE,
                      "pages": pages})
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")
    assert store.subscribers() == []


def test_a_field_other_than_pages_is_still_refused(client):  # noqa: F811
    app, store = client
    answer = _signed(app, fiki.Key.generate(), "POST", SUBSCRIPTION,
                     {"callback": "http://127.0.0.1:9000/batch", "nonce": NONCE,
                      "pages": 65536, "extra": 1})
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")


def test_the_chain_bytes_survive_paging(store):
    store.publish(registry="E00000", issuer="EIssuer", digest="d0", chain=b"\x00\xffrip")
    _fill(store, 0)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    head, = _verify(store, *transport.sent[0])["heads"]
    assert base64.b64decode(head["chain"]) == b"\x00\xffrip"


def test_pages_are_sent_only_while_the_delivery_deadline_lasts(store):
    """The pages of one batch share its one delivery deadline rather than one each."""

    class Slow(Recorder):
        timeout = 0.0

        def send(self, url, body, headers, *, deadline=None):
            super().send(url, body, headers)

    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Slow()
    assert Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick() == 0
    assert len(transport.sent) == 1


def test_a_head_past_the_ceiling_but_within_the_declared_size_travels_alone(store, monkeypatch):
    """Codex on #45: the Registrar's ceiling is its own preference for page size, not a bound the
    subscriber set, so a head larger than the ceiling but within what the subscriber reads goes
    out alone rather than in a page the subscriber also could read but the Registrar would not."""
    monkeypatch.setattr(batching, "PAGE_CEILING", 8192)
    store.publish(registry="E00000", issuer="EIssuer", digest="d0", chain=b"x" * 20000)
    store.publish(registry="E00001", issuer="EIssuer", digest="d1", chain=b"y" * 100)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    pages = [_verify(store, *sent) for sent in transport.sent]
    assert [[head["registry"] for head in page["heads"]] for page in pages] == \
        [["E00000"], ["E00001"]]
    assert all(len(body) <= 65536 for _, body, _ in transport.sent)


def test_a_head_larger_than_the_declared_size_is_sent_alone_and_named_in_the_log(
        store, caplog):
    """Nothing the Registrar can send lets that subscriber read it, so it is sent as it was before
    pages, alone, and the log says which registry and how large."""
    store.publish(registry="E00000", issuer="EIssuer", digest="d0", chain=b"x" * 100000)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Recorder()
    with caplog.at_level("WARNING"):
        Batcher(store=store, transport=transport, window=5, clock=lambda: 0.0).tick()
    assert len(transport.sent) == 1
    assert "E00000" in caplog.text and "65536" in caplog.text


@pytest.mark.parametrize("pages", [2**31, 2**63, 10**30])
def test_a_page_size_past_the_largest_storable_is_refused_by_code(client, pages):  # noqa: F811
    """Codex on #45: an integer past SQLite's range reached the store and answered 500."""
    app, store = client
    answer = _signed(app, fiki.Key.generate(), "POST", SUBSCRIPTION,
                     {"callback": "http://127.0.0.1:9000/batch", "nonce": NONCE,
                      "pages": pages})
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")


def test_every_page_of_a_delivery_is_sent_under_the_one_deadline(store):
    """Copilot on #45: the transport started a fresh timeout for every page, so K pages could each
    run a whole timeout. The batch's one deadline is passed to every send instead."""

    class Timed(Recorder):
        timeout = 30.0

        def __init__(self):
            super().__init__()
            self.deadlines = []

        def send(self, url, body, headers, *, deadline=None):
            self.deadlines.append(deadline)
            super().send(url, body, headers)

    _fill(store, 300)
    store.subscribe("EObserver", "http://127.0.0.1:1/a", "nonce-00000000000000", pages=65536)
    transport = Timed()
    assert Batcher(store=store, transport=transport, window=60, clock=lambda: 0.0).tick() == 1
    assert len(transport.deadlines) > 1
    assert len(set(transport.deadlines)) == 1 and transport.deadlines[0] is not None


def test_the_http_transport_gives_up_at_a_deadline_it_is_handed(sink):
    """A send handed a deadline already past does not start a fresh timeout of its own."""
    import time as clock

    started = clock.monotonic()
    with pytest.raises(TimeoutError):
        HttpTransport(timeout=5, policy=LOOPBACK).send(sink, b"{}", {},
                                                       deadline=clock.monotonic() - 1)
    assert clock.monotonic() - started < 1


def test_without_a_deadline_the_http_transport_keeps_its_own_timeout(sink):
    HttpTransport(timeout=5, policy=LOOPBACK).send(sink, b"{}", {})
    assert _Sink.received
