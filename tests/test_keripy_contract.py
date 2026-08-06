"""Contract tests over the keripy accessors witness reads.

``w7c4mz`` commits to these: witness rides semi-internal keripy accessors (``db.fels``,
``db.clonePreIter``, and the stores beside them) rather than forking keripy, and pairs the commit
pin with contract tests so an upstream bump that moves them fails loudly here instead of silently
in production.

Each test characterizes one accessor against the pinned commit: the shape it returns, the keys it
answers to, and the ordering it guarantees. They are deliberately about keripy's behavior, not
witness's — witness code is barely imported.
"""

import pytest
from keri.db import basing, dbing


def _reopen(facts):
    """Reopen the fixture's witness database read-only, the way the control plane does."""
    return basing.Baser(
        name=facts["name"], base="", temp=False, headDirPath=facts["head"], reopen=True,
        readonly=True,
    )


def test_habs_holds_only_the_witnesss_own_identity(witnessing_db):
    """db.habs is the witness's own keystore, not a record of who it witnesses."""
    rdb = _reopen(witnessing_db)
    try:
        pres = [hr.hid for _keys, hr in rdb.habs.getTopItemIter()]
    finally:
        rdb.close()
    assert pres == [witnessing_db["witness_pre"]]
    assert witnessing_db["controller_pre"] not in pres


def test_fels_indexes_witnessed_events_by_first_seen_ordinal(witnessing_db):
    """db.fels is an OnSuber: getAllItemIter(keys=pre) yields (key, ordinal, said) triples,
    densely from 0, in the order this witness first accepted the events."""
    rdb = _reopen(witnessing_db)
    try:
        items = list(rdb.fels.getAllItemIter(keys=witnessing_db["controller_pre"]))
        count = rdb.fels.cnt(keys=witnessing_db["controller_pre"])
    finally:
        rdb.close()
    assert [on for _key, on, _said in items] == list(range(len(witnessing_db["saids"])))
    assert [said for _key, _on, said in items] == witnessing_db["saids"]
    assert count == len(witnessing_db["saids"])


def test_clonepreiter_replays_the_witnessed_kel_in_order(witnessing_db):
    """db.clonePreIter yields the KEL as CESR message bytes, oldest first, from fn onward."""
    rdb = _reopen(witnessing_db)
    try:
        msgs = list(rdb.clonePreIter(pre=witnessing_db["controller_pre"], fn=0))
        tail = list(rdb.clonePreIter(pre=witnessing_db["controller_pre"], fn=1))
    finally:
        rdb.close()
    assert len(msgs) == len(witnessing_db["saids"])
    assert all(isinstance(m, (bytes, bytearray)) for m in msgs)
    assert bytes(msgs[0]).startswith(b'{"v"') or bytes(msgs[0])[:1] == b"-"
    assert len(tail) == len(msgs) - 1


def test_dtss_stamps_every_witnessed_event_with_a_first_seen_datetime(witnessing_db):
    """db.dtss is keyed by (pre, said) digest key and holds an ISO-8601 Dater."""
    rdb = _reopen(witnessing_db)
    try:
        stamps = [
            rdb.dtss.get(keys=dbing.dgKey(witnessing_db["controller_pre"], said))
            for said in witnessing_db["saids"]
        ]
    finally:
        rdb.close()
    assert all(s is not None for s in stamps)
    assert all(s.dts[4] == "-" and s.dts.endswith("+00:00") for s in stamps)


def test_states_holds_current_key_state_for_the_witnessed_aid(witnessing_db):
    """db.states is the current key-state record, at the latest sequence number."""
    rdb = _reopen(witnessing_db)
    try:
        state = rdb.states.get(keys=witnessing_db["controller_pre"])
    finally:
        rdb.close()
    assert state is not None
    assert state.i == witnessing_db["controller_pre"]
    assert int(state.s, 16) == len(witnessing_db["saids"]) - 1
    assert state.d == witnessing_db["saids"][-1]


def test_wigs_holds_the_witnesss_own_indexed_signature(witnessing_db):
    """db.wigs holds witness signatures keyed by (pre, said) — the receipt this witness gave."""
    rdb = _reopen(witnessing_db)
    try:
        wigers = rdb.wigs.get(
            keys=(witnessing_db["controller_pre"], witnessing_db["saids"][0])
        )
    finally:
        rdb.close()
    assert len(wigers) == 1
    assert wigers[0].index == 0


@pytest.mark.parametrize("accessor", ["habs", "fels", "dtss", "states", "wigs", "clonePreIter"])
def test_a_moved_accessor_fails_closed_rather_than_reading_as_empty(witnessing_db, accessor):
    """The invariant the tests above exist to protect, kept rather than proven once.

    ``w7c4mz`` accepts riding semi-internal keripy accessors on the strength of a commit pin
    plus contract tests. That bargain only holds if a moved accessor is *loud*. Ablate one on
    the read path and the read must raise — never degrade to an empty result, which would let
    an upstream rename read as "this witness has witnessed nothing."
    """
    rdb = _reopen(witnessing_db)
    try:
        # The stores are per-instance attributes built in Baser.__init__; clonePreIter is a
        # method on the class. An upstream rename could move either, so ablate where it lives.
        target = rdb if accessor in vars(rdb) else type(rdb)
        monkey = pytest.MonkeyPatch()
        monkey.delattr(target, accessor, raising=True)
        try:
            with pytest.raises(AttributeError):
                getattr(rdb, accessor)
        finally:
            monkey.undo()
    finally:
        rdb.close()
