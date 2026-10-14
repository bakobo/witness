"""The sparse feed's sweep, replay, cursor, and admission contract."""

import base64
import json
from types import SimpleNamespace

import pytest
from falcon import testing
from fiki import Key, Request, sign_request, verify_response
from keri import kering

from witness.app import make_app
from witness import change as change_module
from witness.change import ChangeFeed
from witness.config import ControlPlaneConfig
from witness.errors import CursorConflict
from witness.reader import WitnessReader
from test_app import StubReader

HOST = "falconframework.org"
NOW = 1_800_000_000


class FakeDb:
    def __init__(self):
        self.records = {}
        self.receipts = {}
        self.cloned = []
        self.states = SimpleNamespace(getTopItemIter=self.states_iter, get=self.state_get)
        self.wigs = SimpleNamespace(get=self.wigs_get)

    def states_iter(self):
        for aid, record in self.records.items():
            yield (aid,), record

    def state_get(self, *, keys):
        return self.records.get(keys)

    def wigs_get(self, *, keys):
        return [b"receipt"] * self.receipts.get(keys, 0)

    def clonePreIter(self, pre, fn, gvrsn):
        self.cloned.append((pre, fn, gvrsn.major))
        for ordinal in range(fn, int(self.records[pre.decode()].f, 16) + 1):
            yield f"{pre.decode()}:{ordinal}".encode()

    def close(self):
        pass


def state(db, aid, sn=0, receipts=1):
    said = f"said-{aid}-{sn}"
    db.records[aid] = SimpleNamespace(s=format(sn, "x"), d=said, f=format(sn, "x"))
    db.receipts[(aid, said)] = receipts


@pytest.fixture
def setup(tmp_path):
    db = FakeDb()
    time = [NOW]
    reader = SimpleNamespace(_open=lambda: db)
    feed = ChangeFeed(tmp_path / "change.sqlite", reader, clock=lambda: time[0])
    yield feed, db, time
    feed.close()


def test_sweep_detects_new_event_and_latest_receipt_and_quiet_bucket(setup):
    feed, db, time = setup
    state(db, "A", 0)
    feed.sweep()
    assert feed.page(None, 10)["change"][0]["fn"] == 0

    time[0] += 60
    state(db, "A", 1)
    feed.sweep()
    assert feed.page(feed.cursor_for(time[0]), 10)["change"][0]["fn"] == 1

    db.receipts[("A", "said-A-1")] = 2
    feed.sweep()
    assert feed.page(feed.cursor_for(time[0]), 10)["change"][0]["fn"] == 1
    before = feed.change_count()
    time[0] += 60
    feed.sweep()
    assert feed.change_count() == before


def test_a_real_keripy_replay_is_genus_pinned_and_contains_the_held_attachments(
        witnessing_db, tmp_path):
    cfg = ControlPlaneConfig(name=witnessing_db["name"], host="127.0.0.1", port=1,
                             head_dir_path=witnessing_db["head"], telemetry_path=None)
    reader = WitnessReader(cfg)
    feed = ChangeFeed(tmp_path / "real.sqlite", reader, clock=lambda: NOW)
    try:
        feed.sweep()
        rows = {row["aid"]: row for row in feed.page(None, 20)["change"]}
        aid = witnessing_db["controller_pre"]
        with_db = reader._open()
        try:
            expected = b"".join(with_db.clonePreIter(pre=aid.encode(), fn=0,
                                                     gvrsn=kering.Versionage(2, 0)))
        finally:
            with_db.close()
        assert base64.b64decode(rows[aid]["kel"]) == expected
        assert len(expected) > 0
    finally:
        feed.close()


def test_one_page_returns_all_changed_aids_from_the_right_fn(setup):
    feed, db, time = setup
    for aid in ("A", "B", "C"):
        state(db, aid)
    feed.sweep()
    time[0] += 60
    since = feed.cursor_for(time[0])
    state(db, "A", 1)
    state(db, "B", 1)
    feed.sweep()
    answer = feed.page(since, 10)
    assert [(row["aid"], row["fn"]) for row in answer["change"]] == [("A", 1), ("B", 1)]
    assert all(row["kel"] for row in answer["change"])
    assert db.cloned[-2:] == [(b"A", 1, 2), (b"B", 1, 2)]


def test_current_bucket_is_resent_by_the_final_cursor(setup):
    feed, db, _time = setup
    state(db, "A")
    feed.sweep()
    first = feed.page(None, 10)
    second = feed.page(first["next"], 10)
    assert second["change"] == first["change"]


def test_an_empty_delta_neither_opens_lmdb_nor_adds_a_change_row(setup):
    feed, db, time = setup
    time[0] += 60
    answer = feed.page(feed.cursor_for(time[0]), 10)
    assert answer["change"] == []
    assert answer["more"] is False
    assert feed.change_count() == 0


def test_pages_honor_limit_and_continue_a_full_listing(setup):
    feed, db, _time = setup
    for aid in ("A", "B", "C"):
        state(db, aid)
    feed.sweep()
    first = feed.page(None, 2)
    second = feed.page(first["next"], 2)
    assert first["more"] is True
    assert second["more"] is False
    assert [row["aid"] for row in first["change"] + second["change"]] == ["A", "B", "C"]


def test_a_change_arriving_between_pages_is_seen_and_the_first_bucket_is_rechecked(setup):
    feed, db, time = setup
    for aid in ("A", "C"):
        state(db, aid)
    feed.sweep()
    first = feed.page(None, 1)
    state(db, "B")
    state(db, "A", 1)
    feed.sweep()
    second = feed.page(first["next"], 1)
    third = feed.page(second["next"], 1)
    assert [row["aid"] for row in first["change"] + second["change"] + third["change"]] \
        == ["A", "B", "C"]
    # The final cursor is fixed to the first answer's bucket, so A's later change is resent.
    assert feed.page(third["next"], 10)["change"][0]["aid"] == "A"


def test_foreign_and_expired_cursors_refuse(setup, tmp_path):
    feed, db, time = setup
    state(db, "A")
    feed.sweep()
    cursor = feed.page(None, 10)["next"]
    other = ChangeFeed(tmp_path / "other.sqlite", SimpleNamespace(_open=lambda: db),
                       clock=lambda: time[0])
    try:
        with pytest.raises(CursorConflict):
            other.page(cursor, 10)
    finally:
        other.close()
    time[0] += 8 * 86400
    feed.sweep()
    with pytest.raises(CursorConflict):
        feed.page(cursor, 10)


def test_rollback_and_a_removed_aid_each_start_a_new_generation(setup):
    feed, db, time = setup
    state(db, "A", 2)
    state(db, "B")
    feed.sweep()
    first = feed.page(None, 10)["next"]
    time[0] += 60
    state(db, "A", 1)
    feed.sweep()
    with pytest.raises(CursorConflict):
        feed.page(first, 10)
    second = feed.page(None, 10)["next"]
    del db.records["B"]
    feed.sweep()
    with pytest.raises(CursorConflict):
        feed.page(second, 10)


def test_lost_latest_receipt_starts_a_new_generation(setup):
    feed, db, _time = setup
    state(db, "A", receipts=2)
    feed.sweep()
    cursor = feed.page(None, 10)["next"]
    db.receipts[("A", "said-A-0")] = 1
    feed.sweep()
    with pytest.raises(CursorConflict):
        feed.page(cursor, 10)


def test_a_new_head_with_fewer_receipts_does_not_reset_generation(setup):
    feed, db, time = setup
    state(db, "A", receipts=2)
    feed.sweep()
    first_generation = feed.generation
    time[0] += 60
    state(db, "A", sn=1, receipts=1)
    feed.sweep()
    assert feed.generation == first_generation
    assert feed.page(feed.cursor_for(time[0]), 10)["change"][0]["fn"] == 1


def test_index_and_signing_identity_survive_a_control_plane_restart(tmp_path):
    db = FakeDb()
    state(db, "A")
    path = tmp_path / "change.sqlite"
    reader = SimpleNamespace(_open=lambda: db)
    first = ChangeFeed(path, reader, clock=lambda: NOW)
    first.sweep()
    key_aid = first.key().aid
    cursor = first.page(None, 10)["next"]
    first.close()
    second = ChangeFeed(path, reader, clock=lambda: NOW)
    try:
        assert second.key().aid == key_aid
        with pytest.raises(CursorConflict):
            second.page(cursor, 10)
        assert second.page(None, 10)["change"][0]["aid"] == "A"
    finally:
        second.close()


def test_a_disappearing_aid_never_earns_a_200_with_an_empty_replay(setup):
    feed, db, _time = setup
    state(db, "A")
    feed.sweep()

    def vanished(*_args, **_kwargs):
        return iter(())

    db.clonePreIter = vanished
    with pytest.raises(Exception) as refused:
        feed.page(None, 10)
    assert refused.value.status == 503


@pytest.mark.parametrize("mutation", ["remove", "lower_sn", "lower_fn", "different_said"])
def test_a_state_changed_behind_the_index_refuses_before_advancing_cursor(setup, mutation):
    feed, db, _time = setup
    state(db, "A", sn=2)
    feed.sweep()
    if mutation == "remove":
        del db.records["A"]
    elif mutation == "lower_sn":
        db.records["A"].s = "1"
    elif mutation == "lower_fn":
        db.records["A"].f = "1"
    else:
        db.records["A"].d = "another-said"
    with pytest.raises(Exception) as refused:
        feed.page(None, 10)
    assert refused.value.status == 503


def test_a_partial_keripy_replay_refuses_instead_of_claiming_the_page_is_complete(setup):
    feed, db, _time = setup
    state(db, "A", sn=2)
    feed.sweep()
    db.clonePreIter = lambda **_kwargs: iter((b"only one event",))
    with pytest.raises(Exception) as refused:
        feed.page(None, 10)
    assert refused.value.status == 503


def test_replay_budget_bounds_a_page_even_when_aid_limit_is_valid(setup, monkeypatch):
    feed, db, _time = setup
    for aid in ("A", "B"):
        state(db, aid)
    feed.sweep()
    monkeypatch.setattr(change_module, "MAX_PAGE_REPLAY_BYTES", 3)
    with pytest.raises(Exception) as refused:
        feed.page(None, 2)
    assert refused.value.status == 413


def test_sweep_worker_refuses_pages_after_failure_and_recovers(setup):
    feed, db, _time = setup
    state(db, "A")
    first = [True, True, False]

    class Stop:
        def wait(self, _seconds):
            return not first.pop(0)

    stop = feed._stop
    feed._stop = Stop()
    real_sweep = feed.sweep
    calls = [0]

    def sometimes_fails():
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("database unavailable")
        real_sweep()

    feed.sweep = sometimes_fails
    feed._run()
    feed._stop = stop
    assert calls[0] == 2
    assert feed.page(None, 10)["change"][0]["aid"] == "A"


def test_a_failed_initial_sweep_keeps_the_control_plane_alive_and_refuses_feed(tmp_path):
    reader = SimpleNamespace(_open=lambda: (_ for _ in ()).throw(RuntimeError("no DB")))
    feed = ChangeFeed(tmp_path / "change.sqlite", reader, clock=lambda: NOW)
    try:
        feed.start()
        with pytest.raises(Exception) as refused:
            feed.page(None, 10)
        assert refused.value.status == 503
    finally:
        feed.close()


def test_an_unreadable_index_refuses_with_a_typed_retryable_error(setup):
    feed, _db, _time = setup
    feed._db.close()
    with pytest.raises(Exception) as refused:
        feed.page(None, 10)
    assert (refused.value.status, refused.value.code) == (503, "e.env.change.unavailable.r")


def test_a_generation_change_during_replay_refuses_the_page(setup):
    feed, db, _time = setup
    state(db, "A")
    feed.sweep()
    original = db.clonePreIter

    def replaced_during_replay(*args, **kwargs):
        feed.generation = "replacement"
        yield from original(*args, **kwargs)

    db.clonePreIter = replaced_during_replay
    with pytest.raises(CursorConflict):
        feed.page(None, 10)


def test_bad_cursor_shapes_and_limits_refuse_without_querying(setup):
    feed, _db, time = setup
    bad_values = ["", "x" * 1025, "!!!", "é", base64.urlsafe_b64encode(b"no-json").decode(),
                  base64.urlsafe_b64encode(b"{}").decode()]
    for bad in bad_values:
        with pytest.raises(Exception) as refused:
            feed.page(bad, 1)
        assert refused.value.status == 400
    for values in ([feed.generation, "1", None, None],
                   [feed.generation, time[0] // 60, "A", None],
                   [feed.generation, time[0] // 60, None, 1],
                   [feed.generation, -2, None, None],
                   [feed.generation, time[0] // 60 + 1, None, None]):
        bad = base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")
        with pytest.raises(Exception) as refused:
            feed.page(bad, 1)
        assert refused.value.status == 400
    for bad_limit in (0, 1001, "1", True):
        with pytest.raises(Exception) as refused:
            feed.page(None, bad_limit)
        assert refused.value.status == 400


def test_feed_requires_watcher_role_and_signs_the_bound_response(setup):
    feed, db, _time = setup
    state(db, "A")
    feed.sweep()
    watcher = Key.generate()
    operator = Key.generate()
    route = "/v1/witness/change?limit=1"
    signed = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    kwargs = dict(change_feed=feed, operators=(operator.aid,), clock=lambda: NOW)
    client = testing.TestClient(make_app(StubReader(), watchers=(watcher.aid,), **kwargs))
    assert client.simulate_get(route).status_code == 401
    operator_headers = sign_request(key=operator, method="GET", url=f"http://{HOST}{route}",
                                    created=NOW)
    assert client.simulate_get(route, headers=operator_headers).status_code == 403
    assert testing.TestClient(make_app(StubReader(), watchers=(), **kwargs)).simulate_get(
        route, headers=signed).status_code == 403
    response = client.simulate_get(route, headers=signed)
    assert response.status_code == 200
    assert len(response.json["change"]) == 1
    verify_response(status=200, headers=response.headers, body=response.content,
                    request=Request("GET", f"http://{HOST}{route}", signed),
                    expected_aid=feed.key().aid, max_age=60, now=NOW, expected_keyid=None)


def test_feed_signs_a_retryable_refusal_with_retry_after(setup):
    feed, _db, _time = setup
    feed._failed = True
    watcher = Key.generate()
    route = "/v1/witness/change"
    signed = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    response = client.simulate_get(route, headers=signed)
    assert (response.status_code, response.json["code"]) == (503, "e.env.change.unavailable.r")
    assert response.headers["retry-after"] == "5"
    verify_response(status=503, headers=response.headers, body=response.content,
                    request=Request("GET", f"http://{HOST}{route}", signed),
                    expected_aid=feed.key().aid, max_age=60, now=NOW, expected_keyid=None)


def test_expired_cursor_is_a_signed_409_problem_document(setup):
    feed, db, time = setup
    state(db, "A")
    feed.sweep()
    cursor = feed.page(None, 10)["next"]
    time[0] += 8 * 86400
    feed.sweep()
    watcher = Key.generate()
    route = f"/v1/witness/change?since={cursor}"
    signed = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    response = client.simulate_get(route, headers=signed)
    assert (response.status_code, response.json["code"]) == (409, "e.state.conflict.cursor.f")
    assert response.headers["content-type"].startswith("application/problem+json")
    verify_response(status=409, headers=response.headers, body=response.content,
                    request=Request("GET", f"http://{HOST}{route}", signed),
                    expected_aid=feed.key().aid, max_age=60, now=NOW, expected_keyid=None)


@pytest.mark.parametrize("query", ["limit=0", "limit=-1", "limit=999999", "limit=abc",
                                    "since=!!!", "since=" + "a" * 1025,
                                    "since=" + "a" * 2050,
                                    "limit=1&limit=2", "extra=1", "x=1&y=2&z=3&v=4"])
def test_query_values_are_bounded_before_use(setup, query):
    feed, _db, _time = setup
    watcher = Key.generate()
    route = f"/v1/witness/change?{query}"
    headers = sign_request(key=watcher, method="GET", url=f"http://{HOST}{route}", created=NOW)
    client = testing.TestClient(make_app(StubReader(), change_feed=feed,
                                         watchers=(watcher.aid,), clock=lambda: NOW))
    assert client.simulate_get(route, headers=headers).status_code == 400
