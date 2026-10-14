"""The log-backed feed's paging, replay, cursor, and admission contract."""

import base64
import json
import shutil
from types import SimpleNamespace

import pytest
from keri import kering
from falcon import testing
from fiki import Key, Request, sign_request, verify_response

from witness.app import make_app
from witness import change
from witness.changelog import ChangeLog
from witness.config import ControlPlaneConfig
from witness.errors import ChangeInput, ChangePageTooLarge, ChangeUnavailable, CursorConflict
from witness.reader import WitnessReader
from test_app import StubReader

HOST = "falconframework.org"
NOW = 1_800_000_000


def cursor(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


class FakeCursor:
    def __init__(self, records):
        self.rows = sorted((aid.encode(), state) for aid, state in records.items())

    def set_range(self, key):
        self.rows = [(k, v) for k, v in self.rows if k >= key]
        return bool(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeDb:
    def __init__(self):
        self.records = {}
        self.fon = {}
        self.cloned = []
        self.states = SimpleNamespace(sdb=object(), _des=lambda value: value,
                                      get=lambda *, keys: self.records.get(keys))
        self.fons = SimpleNamespace(get=lambda *, keys: self.fon.get(keys))
        self.fels = SimpleNamespace(getAllItemIter=self.fels_iter)
        self.missing = set()
        self.unclonable = set()
        self.env = SimpleNamespace(begin=lambda: self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self, db):
        assert db is self.states.sdb
        return FakeCursor(self.records)

    def fels_iter(self, *, keys, on):
        aid = keys.decode()
        for n in range(on, int(self.records[aid].f, 16) + 1):
            if n not in self.missing:
                yield (aid,), n, f"{aid}-{n}".encode()

    def cloneEvtMsg(self, *, pre, fn, dig, gvrsn):
        if fn in self.unclonable:
            raise kering.MissingEntryError("no sigs")
        self.cloned.append((pre.decode(), fn, gvrsn.major))
        return f"{pre.decode()}:{fn}".encode()

    def close(self):
        pass


def state(db, aid, fn=0):
    db.records[aid] = SimpleNamespace(f=format(fn, "x"))
    for n in range(fn + 1):
        db.fon[(aid, f"{aid}-{n}")] = SimpleNamespace(num=n)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "change"
    writer = ChangeLog.open(path)
    generation = writer.generation
    writer.close_clean()

    class Log:
        def append(self, aid, digest, fn=None):
            opened = ChangeLog.open(path)
            try:
                return opened.append(aid, digest, fn)
            finally:
                opened.close_clean()

    log = Log()
    log.generation = generation
    db, opens = FakeDb(), []

    def open_db():
        opens.append(1)
        return db

    feed = change.ChangeFeed(lambda: path, SimpleNamespace(_open=open_db))
    yield log, feed, db, opens


def test_quiet_poll_never_opens_baser(setup):
    log, feed, db, opens = setup
    state(db, "A")
    log.append("A", "A-0")
    last = feed.page(None, 10)["next"]
    opens.clear()
    db.cloned.clear()
    assert feed.page(last, 10)["change"] == []
    assert opens == [] and db.cloned == []


def test_initial_sync_pages_and_catches_changes_during_copy(setup):
    log, feed, db, _opens = setup
    for aid in "AC":
        state(db, aid)
    log.append("A", "A-0")
    first = feed.page(None, 1)
    state(db, "B")
    state(db, "A", 1)
    log.append("A", "A-1")
    second = feed.page(first["next"], 1)
    third = feed.page(second["next"], 1)
    assert [r["aid"] for page in (first, second, third) for r in page["change"]] == ["A", "B", "C"]
    assert not third["more"]
    assert [(r["aid"], r["fn"]) for r in feed.page(third["next"], 10)["change"]] == [("A", 1)]


def test_poll_keeps_min_fn_and_resends_after_boundary(setup):
    log, feed, db, _opens = setup
    for aid in "AB":
        state(db, aid, 2)
    baseline = feed.page(None, 10)["next"]
    for aid, n in (("A", 2), ("A", 1), ("B", 2), ("A", 2)):
        log.append(aid, f"{aid}-{n}")
    first = feed.page(baseline, 1)
    second = feed.page(first["next"], 1)
    third = feed.page(second["next"], 1)
    assert [(r["aid"], r["fn"]) for r in first["change"]] == [("A", 1)]
    assert [(r["aid"], r["fn"]) for r in second["change"]] == [("B", 2)]
    assert [(r["aid"], r["fn"]) for r in third["change"]] == [("A", 2)]


def test_escrow_and_entry_bound(setup, monkeypatch):
    log, feed, db, _opens = setup
    state(db, "A")
    baseline = feed.page(None, 10)["next"]
    log.append("X", "escrow")
    log.append("A", "A-0")
    monkeypatch.setattr(change, "MAX_PAGE_ENTRIES", 1)
    first = feed.page(baseline, 10)
    assert first["change"] == [] and first["more"]
    assert feed.page(first["next"], 10)["change"][0]["aid"] == "A"


def test_a_poll_never_consumes_a_first_seen_event_keripy_is_still_writing(setup):
    """keripy pins fons after fels.append; a poll in that gap must not step past the entry."""
    log, feed, db, _opens = setup
    state(db, "A")
    baseline = feed.page(None, 10)["next"]
    log.append("B", "B-0", 0)
    early = feed.page(baseline, 10)
    assert early["change"] == [] and early["more"]
    assert early["next"] == baseline
    state(db, "B")
    assert [(r["aid"], r["fn"]) for r in feed.page(early["next"], 10)["change"]] == [("B", 0)]


def test_a_poll_refuses_while_key_state_lags_an_entrys_fons(setup):
    """fons is pinned before keripy's key state and KEL move; the replay must reach the entry."""
    log, feed, db, _opens = setup
    state(db, "A")
    baseline = feed.page(None, 10)["next"]
    db.fon[("A", "A-1")] = SimpleNamespace(num=1)
    log.append("A", "A-1", 1)
    with pytest.raises(ChangeUnavailable):
        feed.page(baseline, 10)
    state(db, "A", 1)
    assert [(r["aid"], r["fn"]) for r in feed.page(baseline, 10)["change"]] == [("A", 1)]


def test_a_writer_failure_during_a_page_refuses_that_page(setup, tmp_path):
    log, feed, db, opens = setup
    state(db, "A")
    baseline = feed.page(None, 10)["next"]
    log.append("A", "A-0")
    original = feed.reader._open

    def failing_open():
        (tmp_path / "change" / "live").unlink()
        return original()

    feed.reader._open = failing_open
    with pytest.raises(ChangeUnavailable, match="during this request"):
        feed.page(baseline, 10)


def test_a_receipt_on_an_unaccepted_event_is_consumed_without_a_replay(setup):
    log, feed, db, _opens = setup
    state(db, "A")
    baseline = feed.page(None, 10)["next"]
    log.append("B", "escrowed")
    page = feed.page(baseline, 10)
    assert page["change"] == [] and not page["more"]


def test_foreign_and_past_cursor_conflict(setup):
    log, feed, db, _opens = setup
    state(db, "A")
    log.append("A", "A-0")
    for bad in (cursor(["foreign", 0, "00" * 16, None]),
                cursor([log.generation, 99, "00" * 16, None]),
                cursor([log.generation, 1, "ff" * 16, None])):
        with pytest.raises(CursorConflict):
            feed.page(bad, 10)


def test_restored_log_with_a_different_entry_refuses_cursor(tmp_path):
    path = tmp_path / "change"
    log = ChangeLog.open(path)
    log.append("A", "A-0")
    log.close_clean()
    shutil.copytree(path, tmp_path / "snapshot")
    log = ChangeLog.open(path)
    log.append("B", "B-0")
    log.close_clean()
    feed = change.ChangeFeed(lambda: path, SimpleNamespace(_open=lambda: pytest.fail("Baser opened")))
    from witness.changelog import LogReader
    with LogReader(path) as reader:
        ordinal, chain = reader.last()
        stale = cursor([reader.generation, ordinal, chain.hex(), None])
    shutil.rmtree(path)
    shutil.copytree(tmp_path / "snapshot", path)
    log = ChangeLog.open(path)
    log.append("C", "C-0")
    log.close_clean()
    with pytest.raises(CursorConflict):
        feed.page(stale, 10)


def test_bad_cursor_and_limit_refuse_before_read():
    feed = change.ChangeFeed(lambda: pytest.fail("path resolved"), SimpleNamespace(_open=lambda: pytest.fail("Baser opened")))
    for bad in ("", "x" * 1025, "!!!", cursor({}), cursor(["g", "1", "00" * 16, None]),
                cursor(["g", 0, "wrong", None]), cursor(["g", -1, "00" * 16, None])):
        with pytest.raises(ChangeInput):
            feed.page(bad, 1)
    for limit in (0, 1001, "1", True):
        with pytest.raises(ChangeInput):
            feed.page(None, limit)


def test_missing_log_is_a_retryable_503(tmp_path):
    feed = change.ChangeFeed(lambda: tmp_path / "absent", SimpleNamespace(_open=lambda: pytest.fail("Baser opened")))
    with pytest.raises(ChangeUnavailable) as refused:
        feed.page(None, 1)
    assert (refused.value.status, refused.value.retryable) == (503, True)
    assert not (tmp_path / "absent").exists()


def test_corrupt_log_open_is_a_retryable_503(monkeypatch):
    import lmdb

    def broken(_path):
        raise lmdb.Error("unreadable")

    monkeypatch.setattr(change, "LogReader", broken)
    feed = change.ChangeFeed(lambda: "/any", SimpleNamespace(_open=lambda: pytest.fail("Baser opened")))
    with pytest.raises(ChangeUnavailable):
        feed.key()


def test_empty_initial_sync_has_a_poll_cursor(setup):
    _log, feed, _db, _opens = setup
    first = feed.page(None, 1)
    assert first["change"] == [] and not first["more"]
    assert feed.page(first["next"], 1)["change"] == []


def test_missing_key_state_refuses_replay(setup):
    log, feed, db, _opens = setup
    state(db, "A")
    log.append("A", "A-0")
    first = feed.page(None, 1)
    del db.records["A"]
    with pytest.raises(ChangeUnavailable):
        feed.page(cursor([log.generation, 0, "00" * 16, None]), 1)
    assert first["change"]


def test_a_gap_in_first_seen_order_refuses_even_when_later_events_fill_the_count(setup):
    log, feed, db, _opens = setup
    state(db, "A", 3)
    db.missing = {2}
    with pytest.raises(ChangeUnavailable, match="gap"):
        feed.page(None, 10)


def test_a_replay_short_of_the_key_state_refuses(setup):
    log, feed, db, _opens = setup
    state(db, "A", 2)
    db.missing = {2}
    with pytest.raises(ChangeUnavailable, match="every indexed event"):
        feed.page(None, 10)


def test_incomplete_replay_and_budget_refuse(setup, monkeypatch):
    log, feed, db, _opens = setup
    state(db, "A", 1)
    log.append("A", "A-0")
    db.unclonable = {1}
    with pytest.raises(ChangeUnavailable, match="replay an indexed event"):
        feed.page(None, 10)
    db.unclonable = set()
    monkeypatch.setattr(change, "MAX_PAGE_REPLAY_BYTES", 2)
    with pytest.raises(ChangePageTooLarge):
        feed.page(None, 10)


def test_auth_and_signed_conflict(setup):
    log, feed, db, _opens = setup
    state(db, "A")
    log.append("A", "A-0")
    watcher, operator = Key.generate(), Key.generate()
    route = "/v1/witness/change?limit=1"
    signed = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    kwargs = dict(change_feed=feed, operators=(operator.aid,), clock=lambda: NOW)
    client = testing.TestClient(make_app(StubReader(), watchers=(watcher.aid,), **kwargs))
    assert client.simulate_get(route).status_code == 401
    op = sign_request(key=operator, method="GET", url=f"http://{HOST}{route}", created=NOW)
    assert client.simulate_get(route, headers=op).json["code"] == "e.grant.missing.watcher.f"
    no_watchers = testing.TestClient(make_app(StubReader(), watchers=(), **kwargs))
    assert no_watchers.simulate_get(route, headers=signed).status_code == 403
    assert no_watchers.simulate_get("/v1/witness/health").status_code == 200
    assert no_watchers.simulate_get("/v1/witness/identity").json["change_key"] == feed.key().aid
    response = client.simulate_get(route, headers=signed)
    assert response.status_code == 200
    verify_response(status=200, headers=response.headers, body=response.content,
                    request=Request("GET", f"http://{HOST}{route}", signed),
                    expected_aid=feed.key().aid, max_age=60, now=NOW, expected_keyid=None)
    bad_route = f"/v1/witness/change?since={cursor(['foreign', 0, '00' * 16, None])}"
    bad_headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{bad_route}", created=NOW)
    refused = client.simulate_get(bad_route, headers=bad_headers)
    assert (refused.status_code, refused.json["code"]) == (409, "e.state.conflict.cursor.f")
    verify_response(status=409, headers=refused.headers, body=refused.content,
                    request=Request("GET", f"http://{HOST}{bad_route}", bad_headers),
                    expected_aid=feed.key().aid, max_age=60, now=NOW, expected_keyid=None)


@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "limit=1001", "limit=abc",
                                    "limit=1&limit=2", "since=!!!", "since=" + "a" * 1025,
                                    "since=" + "a" * 2050, "extra=1", "x=1&y=2&z=3&v=4"])
def test_http_query_values_are_bounded_before_read(setup, query):
    _log, feed, _db, opens = setup
    watcher = Key.generate()
    route = f"/v1/witness/change?{query}"
    headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    assert client.simulate_get(route, headers=headers).status_code == 400
    assert opens == []


def test_retryable_log_refusal_is_signed(setup):
    log, feed, _db, _opens = setup
    key_aid = feed.key().aid
    # A failed writer leaves its failed marker for the reader; the key remains available in memory.
    path = feed.path_fn()
    opened = ChangeLog.open(path)
    with opened.env.begin(write=True) as txn:
        txn.put(b"failed", b"1", db=opened.meta)
    opened.close()
    watcher = Key.generate()
    route = "/v1/witness/change"
    headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    refused = client.simulate_get(route, headers=headers)
    assert (refused.status_code, refused.json["code"]) == (503, "e.env.change.unavailable.r")
    assert refused.headers["retry-after"] == "5"
    verify_response(status=503, headers=refused.headers, body=refused.content,
                    request=Request("GET", f"http://{HOST}{route}", headers),
                    expected_aid=key_aid, max_age=60, now=NOW, expected_keyid=None)


def test_a_log_missing_before_any_key_exists_is_a_typed_503(tmp_path):
    feed = change.ChangeFeed(lambda: tmp_path / "absent", SimpleNamespace(_open=lambda: pytest.fail("Baser opened")))
    watcher = Key.generate()
    route = "/v1/witness/change"
    headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    refused = client.simulate_get(route, headers=headers)
    assert (refused.status_code, refused.json["code"]) == (503, "e.env.change.unavailable.r")
    ungranted = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                            watchers=(), clock=lambda: NOW))
    denied = ungranted.simulate_get(route, headers=headers)
    assert (denied.status_code, denied.json["code"]) == (403, "e.grant.missing.watcher.f")


def test_log_disappearing_after_page_is_a_typed_503():
    watcher = Key.generate()
    route = "/v1/witness/change"
    headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)

    def missing_key():
        raise ChangeUnavailable("The change log disappeared before I could sign the page.")

    feed = SimpleNamespace(page=lambda _since, _limit: {"change": [], "more": False},
                           key=missing_key)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    refused = client.simulate_get(route, headers=headers)
    assert (refused.status_code, refused.json["code"]) == (503, "e.env.change.unavailable.r")


def test_real_keripy_states_seek_and_fons_replay(witnessing_db, tmp_path):
    path = tmp_path / "change"
    log = ChangeLog.open(path)
    log.close_clean()
    cfg = ControlPlaneConfig(name=witnessing_db["name"], host="127.0.0.1", port=1,
                             head_dir_path=witnessing_db["head"], telemetry_path=None)
    feed = change.ChangeFeed(lambda: path, WitnessReader(cfg))
    first = feed.page(None, 1)
    pages = [first]
    while pages[-1]["more"]:
        pages.append(feed.page(pages[-1]["next"], 1))
    assert all(len(page["change"]) == 1 for page in pages)
    log = ChangeLog.open(path)
    log.append(witnessing_db["controller_pre"], witnessing_db["saids"][1])
    log.close_clean()
    poll = feed.page(pages[-1]["next"], 10)
    row = next(r for r in poll["change"] if r["aid"] == witnessing_db["controller_pre"])
    assert row["fn"] == 1
    assert base64.b64decode(row["kel"])
