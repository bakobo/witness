"""The Registrar's store: extend-only heads, subscriptions and batch numbering, across restarts."""

import sqlite3

import pytest

from witness.errors import RegistrarFork, RegistrarStale
from witness.registrar.store import RegistrarStore


def _publish(store, registry, chain, *, issuer="EIssuer"):
    return store.publish(registry=registry, issuer=issuer, digest=chain.hex(), chain=chain)


@pytest.fixture
def store(tmp_path):
    opened = RegistrarStore(tmp_path / "registrar.sqlite3")
    yield opened
    opened.close()


def test_a_head_is_kept_only_while_each_snapshot_extends_it(store):
    assert _publish(store, "r1", b"rip") == b"rip".hex()
    assert _publish(store, "r1", b"rip+iss") == b"rip+iss".hex()
    assert _publish(store, "r1", b"rip+iss") == b"rip+iss".hex(), "a repeat is re-acknowledged"
    with pytest.raises(RegistrarStale) as stale:
        _publish(store, "r1", b"rip")
    assert stale.value.code == "e.state.conflict.registrar.stale.f"
    with pytest.raises(RegistrarFork) as fork:
        _publish(store, "r1", b"rip+other")
    assert fork.value.code == "e.state.conflict.registrar.fork.f"
    assert store.head("r1")["chain"] == b"rip+iss"
    assert store.head("unknown") is None


def test_a_head_keeps_no_issuer_kel(store):
    """No reader uses the KEL a publication used to carry (@23vjzsf0)."""
    _publish(store, "r1", b"rip")
    assert store.head("r1") == {"registry": "r1", "issuer": "EIssuer", "digest": b"rip".hex(),
                                "chain": b"rip"}


def test_opening_a_store_drops_the_kel_its_heads_held_and_gives_the_space_back(tmp_path):
    """Every head used to carry its issuer's whole KEL, so one Registrar held 582 MB of it after
    1200 credentials (@23vjzsf0). Opening drops the column and compacts the file once."""
    path = tmp_path / "registrar.sqlite3"
    kel = b"k" * (256 * 1024)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE heads (registry TEXT PRIMARY KEY, issuer TEXT NOT NULL, "
                   "digest TEXT NOT NULL, chain BLOB NOT NULL, kel BLOB NOT NULL, "
                   "changed INTEGER NOT NULL)")
        db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('change', 16)")
        for index in range(16):
            db.execute("INSERT INTO heads VALUES (?, 'EIssuer', ?, ?, ?, ?)",
                       (f"r{index}", f"d{index}", f"rip-{index}".encode(), kel, index + 1))
    assert path.stat().st_size > 16 * len(kel)
    store = RegistrarStore(path)
    try:
        assert store.head("r3") == {"registry": "r3", "issuer": "EIssuer", "digest": "d3",
                                    "chain": b"rip-3"}
        assert _publish(store, "r3", b"rip-3+iss") == b"rip-3+iss".hex()
    finally:
        store.close()
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(heads)")}
    assert columns == {"registry", "issuer", "digest", "chain", "changed"}
    assert path.stat().st_size < len(kel)
    RegistrarStore(path).close()  # nothing left to migrate


def test_a_store_whose_compaction_failed_is_compacted_on_a_later_open(tmp_path):
    """Dropping the KEL commits before the file is compacted. A compaction that failed then
    (a full disk, a crash) leaves the column gone and the space still held, so a later open
    compacts by what the file holds free rather than by whether a column was dropped."""
    path = tmp_path / "registrar.sqlite3"
    kel = b"k" * (256 * 1024)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE heads (registry TEXT PRIMARY KEY, issuer TEXT NOT NULL, "
                   "digest TEXT NOT NULL, chain BLOB NOT NULL, kel BLOB NOT NULL, "
                   "changed INTEGER NOT NULL)")
        for index in range(16):
            db.execute("INSERT INTO heads VALUES (?, 'EIssuer', ?, ?, ?, ?)",
                       (f"r{index}", f"d{index}", b"rip", kel, index + 1))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE heads DROP COLUMN kel")  # and no VACUUM after it
    assert path.stat().st_size > 16 * len(kel)
    store = RegistrarStore(path)
    try:
        assert store.head("r3")["chain"] == b"rip"
    finally:
        store.close()
    assert path.stat().st_size < len(kel)


def test_dropping_a_kel_smaller_than_the_rest_still_compacts(tmp_path):
    """The drop's own open compacts however small a share of the file the KEL was."""
    path = tmp_path / "registrar.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE heads (registry TEXT PRIMARY KEY, issuer TEXT NOT NULL, "
                   "digest TEXT NOT NULL, chain BLOB NOT NULL, kel BLOB NOT NULL, "
                   "changed INTEGER NOT NULL)")
        for index in range(16):
            db.execute("INSERT INTO heads VALUES (?, 'EIssuer', ?, ?, ?, ?)",
                       (f"r{index}", f"d{index}", b"c" * 16384, b"k" * 8192, index + 1))
    RegistrarStore(path).close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0


class _VacuumFails(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql == "VACUUM":
            raise sqlite3.OperationalError("database or disk is full")
        return super().execute(sql, *args)


def test_a_compaction_that_fails_does_not_stop_the_store_opening(tmp_path, monkeypatch, caplog):
    """Compaction only gives space back. A full disk must not keep the Registrar from starting
    over heads that are intact; the next open tries again."""
    path = tmp_path / "registrar.sqlite3"
    kel = b"k" * (256 * 1024)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE heads (registry TEXT PRIMARY KEY, issuer TEXT NOT NULL, "
                   "digest TEXT NOT NULL, chain BLOB NOT NULL, kel BLOB NOT NULL, "
                   "changed INTEGER NOT NULL)")
        for index in range(16):
            db.execute("INSERT INTO heads VALUES (?, 'EIssuer', ?, ?, ?, ?)",
                       (f"r{index}", f"d{index}", b"rip", kel, index + 1))
    connect = sqlite3.connect
    with monkeypatch.context() as patch:
        patch.setattr("witness.registrar.store.sqlite3.connect",
                      lambda *args, **kw: connect(*args, factory=_VacuumFails, **kw))
        store = RegistrarStore(path)
        try:
            assert store.head("r3")["chain"] == b"rip"
        finally:
            store.close()
    assert "not compacted" in caplog.text
    assert path.stat().st_size > 16 * len(kel)
    RegistrarStore(path).close()
    assert path.stat().st_size < len(kel)


def test_a_little_free_space_does_not_compact_the_store_on_every_open(tmp_path):
    path = tmp_path / "registrar.sqlite3"
    store = RegistrarStore(path)
    try:
        for index in range(64):
            _publish(store, f"r{index}", bytes([index]) * 4096)
    finally:
        store.close()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM heads WHERE registry='r0'")
    with sqlite3.connect(path) as db:
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
    assert free > 0
    RegistrarStore(path).close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA freelist_count").fetchone()[0] == free


def test_a_subscription_starts_full_then_carries_only_changes(store):
    _publish(store, "r1", b"a")
    _publish(store, "r2", b"b")
    store.subscribe("EObserver", "http://127.0.0.1:1/batch", "nonce-00000000000000")
    first = store.compose("EObserver")
    assert (first.number, first.full) == (1, True)
    assert sorted(head["registry"] for head in first.heads) == ["r1", "r2"]
    quiet = store.compose("EObserver")
    assert (quiet.number, quiet.full, quiet.heads) == (2, False, ())
    _publish(store, "r2", b"b+rev")
    _publish(store, "r3", b"c")
    changed = store.compose("EObserver")
    assert changed.number == 3
    assert sorted((head["registry"], head["chain"]) for head in changed.heads) == \
        [("r2", b"b+rev"), ("r3", b"c")]
    assert store.subscribers() == [("EObserver", "http://127.0.0.1:1/batch")]


def test_unsubscribing_then_subscribing_restarts_numbering_with_a_full_batch(store):
    _publish(store, "r1", b"a")
    store.subscribe("EObserver", "http://127.0.0.1:1/one", "nonce-00000000000000")
    store.compose("EObserver")
    store.compose("EObserver")
    store.unsubscribe("EObserver")
    store.subscribe("EObserver", "http://127.0.0.1:1/two", "nonce-11111111111111")
    again = store.compose("EObserver")
    assert (again.number, again.full, len(again.heads)) == (1, True, 1)
    assert again.subscription == "nonce-11111111111111"
    assert store.subscribers() == [("EObserver", "http://127.0.0.1:1/two")]


def test_unsubscribing_ends_batches_and_reports_whether_there_was_one(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/batch", "nonce-00000000000000")
    assert store.unsubscribe("EObserver") is True
    assert store.unsubscribe("EObserver") is False
    assert store.subscribers() == []


def test_heads_subscriptions_numbers_and_key_survive_a_restart(tmp_path):
    path = tmp_path / "registrar.sqlite3"
    first = RegistrarStore(path)
    aid = first.key().aid
    _publish(first, "r1", b"rip+iss+rev")
    first.subscribe("EObserver", "http://127.0.0.1:1/batch", "nonce-00000000000000")
    first.compose("EObserver")
    first.close()

    second = RegistrarStore(path)
    try:
        assert second.key().aid == aid, "Observers pin the Registrar's AID"
        with pytest.raises(RegistrarStale):
            _publish(second, "r1", b"rip+iss")
        assert second.subscribers() == [("EObserver", "http://127.0.0.1:1/batch")]
        assert second.compose("EObserver").number == 2, "never reuse a batch number"
    finally:
        second.close()


def test_composing_for_an_unknown_subscriber_is_a_named_refusal(store):
    from witness.errors import RegistrarNoSubscription
    with pytest.raises(RegistrarNoSubscription) as missing:
        store.compose("ENobody")
    assert missing.value.code == "e.state.missing.subscription.f"


def test_the_store_holding_the_signing_seed_is_owner_only(tmp_path):
    import os
    import stat
    state = tmp_path / "shared"
    state.mkdir(mode=0o755)
    opened = RegistrarStore(state / "registrar.sqlite3")
    try:
        assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(state / "registrar.sqlite3").st_mode) == 0o600
    finally:
        opened.close()


def test_repeating_a_subscription_replaces_its_nonce_and_continues_its_numbers(store):
    store.subscribe("EObserver", "http://127.0.0.1:1/batch", "nonce-00000000000000")
    assert store.compose("EObserver").subscription == "nonce-00000000000000"
    store.subscribe("EObserver", "http://127.0.0.1:1/batch", "nonce-22222222222222")
    batch = store.compose("EObserver")
    assert (batch.number, batch.subscription) == (2, "nonce-22222222222222")
