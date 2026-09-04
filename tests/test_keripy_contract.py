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


# --- The absent-AID path, and the boundaries an endpoint has to respect -------------------


def test_an_unwitnessed_aid_reads_as_empty_everywhere_rather_than_raising(witnessing_db):
    """The single most important characterization for a data endpoint.

    Asking about an AID this witness has never seen must be a *normal* answer, not an exception.
    It reads as empty across every store — which means calling code cannot distinguish "never
    witnessed" from "read nothing" on the evidence of a single accessor, and must therefore
    decide absence deliberately rather than inferring it.
    """
    absent = witnessing_db["unwitnessed_pre"]
    rdb = _reopen(witnessing_db)
    try:
        fels = list(rdb.fels.getAllItemIter(keys=absent))
        count = rdb.fels.cnt(keys=absent)
        state = rdb.states.get(keys=absent)
        kel = list(rdb.clonePreIter(pre=absent, fn=0))
        wigs = rdb.wigs.get(keys=(absent, witnessing_db["saids"][0]))
    finally:
        rdb.close()
    assert fels == []
    assert count == 0
    assert state is None
    assert kel == []
    assert wigs == []


def test_fels_count_is_the_honest_test_for_whether_an_aid_is_witnessed(witnessing_db):
    """Given the above, `fels.cnt` is the discriminator an endpoint should use: non-zero for a
    witnessed AID, zero for an absent one, and it raises rather than lying if fels ever moves."""
    rdb = _reopen(witnessing_db)
    try:
        witnessed = rdb.fels.cnt(keys=witnessing_db["controller_pre"])
        witnessed2 = rdb.fels.cnt(keys=witnessing_db["controller2_pre"])
        absent = rdb.fels.cnt(keys=witnessing_db["unwitnessed_pre"])
    finally:
        rdb.close()
    assert witnessed == len(witnessing_db["saids"])
    assert witnessed2 == len(witnessing_db["saids2"])
    assert absent == 0


def test_clonepreiter_past_the_end_of_a_kel_yields_nothing_and_does_not_raise(witnessing_db):
    """Paging past the end is a normal request, not an error — an endpoint offering `fn` as a
    cursor needs the empty tail rather than an exception."""
    rdb = _reopen(witnessing_db)
    try:
        beyond = list(rdb.clonePreIter(pre=witnessing_db["controller_pre"], fn=99))
        last = list(rdb.clonePreIter(pre=witnessing_db["controller_pre"],
                                     fn=len(witnessing_db["saids"]) - 1))
    finally:
        rdb.close()
    assert beyond == []
    assert len(last) == 1


def test_two_witnessed_controllers_are_both_enumerable_and_independent(witnessing_db):
    """An index endpoint reads every witnessed AID, and each carries its own ordinal sequence
    starting at zero — the second controller's ordinals do not continue the first's."""
    rdb = _reopen(witnessing_db)
    try:
        first = [on for _k, on, _s in rdb.fels.getAllItemIter(keys=witnessing_db["controller_pre"])]
        second = [on for _k, on, _s in rdb.fels.getAllItemIter(keys=witnessing_db["controller2_pre"])]
    finally:
        rdb.close()
    assert first == list(range(len(witnessing_db["saids"])))
    assert second == list(range(len(witnessing_db["saids2"])))
    assert witnessing_db["controller_pre"] != witnessing_db["controller2_pre"]


def test_a_fully_witnessed_event_carries_every_witnesss_signature(fully_witnessed_db):
    """Under toad=2 the witness's own wigs store holds the full witness set once receipts have
    been exchanged, each signature indexed by its position in the controller's witness list.

    A receipts endpoint has to render this, and the single-witness fixture cannot produce it —
    there, wigs holds exactly one signature at index 0 and the shape looks deceptively simple.
    """
    rdb = _reopen(fully_witnessed_db)
    try:
        wigers = rdb.wigs.get(
            keys=(fully_witnessed_db["controller_pre"], fully_witnessed_db["said"])
        )
    finally:
        rdb.close()
    assert len(wigers) == fully_witnessed_db["toad"]
    assert sorted(w.index for w in wigers) == [0, 1]


def test_states_yields_its_key_as_a_one_element_tuple(witnessing_db):
    """reader._key_states indexes keys[0], and a bare-string key would silently yield whole
    tuples where AIDs are expected — every controller then reported as an unusable value rather
    than as an error. @w7c4mz pairs riding a semi-internal accessor with a test that says so."""
    rdb = _reopen(witnessing_db)
    try:
        keys, _state = next(iter(rdb.states.getTopItemIter()))
    finally:
        rdb.close()

    assert isinstance(keys, tuple)
    assert len(keys) == 1
    assert isinstance(keys[0], str)
