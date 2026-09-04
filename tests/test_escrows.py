"""Pacing keripy's escrow processing (@zj3h2pzh).

Escrow work is O(escrow depth) per hio pass and hio runs 32 passes a second, so the escrow term
dominates a witness's cost long before its ingest capacity does. The cadence is decided by what
the doer YIELDS, and keripy's `escrowDo` ends its loop with a bare `yield`, which `Doist.recur`
reads as "rerun next pass".

Two kinds of test here. The first kind checks the substitution does what it claims. The second is
a CONTRACT test over keripy itself: this module reproduces six lines of upstream, so if upstream
ever adds a fifth escrow, the reproduction would silently stop draining it — and a witness that
stops draining an escrow stops receipting some class of event. That is the failure worth a test
rather than a comment.
"""

import ast
import inspect

import pytest
from hio.base import doing
from keri.app import indirecting

from witness import escrows


class FakeEscrowTarget:
    def __init__(self):
        self.calls = []

    def processEscrows(self):
        self.calls.append("processEscrows")

    def processEscrowReply(self):
        self.calls.append("processEscrowReply")

    def processEscrow(self):
        self.calls.append("processEscrow")


class FakeWitnessStart(doing.DoDoer):
    """Shaped like keripy's WitnessStart: a DoDoer whose sub-doers include a doified escrowDo."""

    def __init__(self, **kwa):
        self.kvy = FakeEscrowTarget()
        self.rvy = FakeEscrowTarget()
        self.tvy = FakeEscrowTarget()
        self.exc = FakeEscrowTarget()
        super().__init__(doers=[doing.doify(self.escrowDo)], **kwa)

    def escrowDo(self, tymth=None, tock=0.0, **kwa):
        self.wind(tymth)
        _ = (yield tock)
        while True:
            self.kvy.processEscrows()
            self.rvy.processEscrowReply()
            self.tvy.processEscrows()
            self.exc.processEscrow()
            yield


def _run(doers, seconds=1.0):
    doing.Doist(limit=seconds, tock=0.03125, real=True, doers=list(doers)).do()


def test_pacing_reduces_how_often_the_escrows_are_swept():
    """The whole point, measured rather than asserted: 32 passes a second becomes one."""
    stock = FakeWitnessStart()
    _run([stock])
    stock_sweeps = stock.kvy.calls.count("processEscrows")

    paced = FakeWitnessStart()
    escrows.pace(([paced]), interval=0.5, kind=FakeWitnessStart)
    _run([paced])

    assert stock_sweeps > 20, f"the stock doer should sweep every pass, saw {stock_sweeps}"
    assert paced.kvy.calls.count("processEscrows") <= 3


def test_pacing_still_sweeps_every_escrow_keripy_sweeps():
    """Pacing must change WHEN, never WHAT. Dropping one escrow would stop a whole class of
    event from ever being receipted, and nothing would raise."""
    paced = FakeWitnessStart()
    escrows.pace([paced], interval=0.1, kind=FakeWitnessStart)

    _run([paced])

    assert paced.kvy.calls == ["processEscrows"] * len(paced.kvy.calls)
    assert paced.rvy.calls and paced.tvy.calls and paced.exc.calls


def test_an_interval_of_zero_leaves_keripy_exactly_as_it_was():
    """The escape hatch has to be genuinely inert, not merely fast, so a deployment that wants
    stock behaviour gets the stock object rather than our imitation of it."""
    subject = FakeWitnessStart()
    before = list(subject.doers)

    assert escrows.pace([subject], interval=0, kind=FakeWitnessStart) is False
    assert subject.doers == before


def test_pacing_a_doer_list_without_a_witness_start_is_a_no_op():
    """`witness control-plane` and any future launcher share this helper; being handed a list that
    holds no WitnessStart is a fact, not an error."""
    assert escrows.pace([doing.Doer()], interval=1.0, kind=FakeWitnessStart) is False


def test_only_the_escrow_doer_is_replaced():
    """Its siblings — start, msgDo, cueDo — are keripy's and stay keripy's."""
    subject = FakeWitnessStart()
    subject.doers = [*subject.doers, doing.doify(subject.escrowDo, name="cueDo")]
    cue = [d for d in subject.doers if d.__name__ == "cueDo"][0]

    escrows.pace([subject], interval=1.0, kind=FakeWitnessStart)

    assert [d for d in subject.doers if d.__name__ == "cueDo"][0] is cue
    assert [d.__name__ for d in subject.doers] == ["escrowDo", "cueDo"]


# ---------------------------------------------------------------------------------------------
# Contract over the six lines of keripy this module reproduces (@w7c4mz)
# ---------------------------------------------------------------------------------------------


def _upstream_escrow_calls():
    """Every `self.<attr>.<method>()` inside keripy's own escrowDo loop."""
    source = inspect.getsource(indirecting.WitnessStart.escrowDo)
    tree = ast.parse(source.lstrip())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name):
            if owner.value.id == "self":
                found.add((owner.attr, node.func.attr))
    return found


def test_the_reproduction_matches_what_keripy_actually_sweeps():
    """@w7c4mz pairs riding upstream with a test that says so.

    If keripy adds a fifth escrow to escrowDo, the paced replacement would keep sweeping only
    four and the fifth would fill forever — no exception, no log, just a class of event that
    stops being receipted. This fails the build instead.
    """
    assert _upstream_escrow_calls() == escrows.UPSTREAM_SWEEPS, (
        "keripy's escrowDo no longer sweeps what witness.escrows reproduces. Update "
        "UPSTREAM_SWEEPS and the paced generator together, and check nothing was dropped."
    )


def test_keripys_escrow_doer_still_ends_its_loop_with_a_bare_yield():
    """The premise of the whole change. A bare yield is what Doist.recur reads as "next pass"; if
    upstream ever yields an interval of its own, pacing here would be redundant at best and would
    fight it at worst."""
    source = inspect.getsource(indirecting.WitnessStart.escrowDo)
    tree = ast.parse(source.lstrip())
    yields = [node for node in ast.walk(tree) if isinstance(node, ast.Yield)]

    assert yields, "escrowDo no longer yields at all"
    assert yields[-1].value is None, (
        "keripy's escrowDo now yields a value, so it sets its own cadence and @zj3h2pzh's "
        "premise needs rechecking"
    )


def test_the_real_witness_start_exposes_a_doified_escrow_doer():
    """Pacing finds the doer by name among WitnessStart.doers. If keripy stops doifying it, or
    renames it, pacing silently does nothing — so the name is part of the contract."""
    source = inspect.getsource(indirecting.WitnessStart.__init__)

    assert "doify(self.escrowDo)" in source.replace(" ", ""), (
        "WitnessStart no longer builds its escrow doer with doify(self.escrowDo); pacing locates "
        "it by that name and would quietly become a no-op"
    )
    assert escrows._DOER_NAME == "escrowDo"


def test_a_witness_without_a_tel_verifier_is_swept_without_crashing():
    """keripy guards `if self.tvy is not None` and so does the reproduction. Dropping that guard
    would take the whole loop down on the first sweep of a witness built without a TEL verifier —
    an exception inside a doer, which is the one thing @c7v3kp forbids most directly."""
    subject = FakeWitnessStart()
    subject.tvy = None
    escrows.pace([subject], interval=0.1, kind=FakeWitnessStart)

    _run([subject])

    assert subject.kvy.calls, "the other escrows must still be swept"
    assert subject.exc.calls
