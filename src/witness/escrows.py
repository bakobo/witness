"""Pacing keripy's escrow sweep (@zj3h2pzh).

Measured: a witness sustains at least 131 legitimate events per second with zero loop lag, while
five unanswerable queries per second cost it 0.077 s of lag. It is not slow; it has one
pathology. Ingest is O(1) per event, but the escrow sweep is O(escrow depth) per hio PASS, and
hio runs 32 passes a second — so at a depth of 300 a witness performs some 9,600 escrow-entry
visits per second against 131 real events.

hio takes a doer's cadence from what it YIELDS: ``Doist.recur`` reads a falsy yield as "rerun on
the next pass". keripy's ``WitnessStart.escrowDo`` ends its loop with a bare ``yield``, so it
sweeps every pass. Note that setting the doer's ``.tock`` does NOT change this, which is the
obvious thing to try: ``escrowDo`` yields its tock on its first yield and bare-yields forever
after, so the attribute delays the first sweep and nothing else.

So this replaces that one entry in ``WitnessStart.doers`` with a generator that sweeps the same
escrows and yields an interval. Nothing in keripy is edited, no class is patched, no module is
monkeypatched — ``DoDoer.doers`` is a settable property read at enter time, on an object
``setupWitness`` handed us.

It is still a step past "add doers, never patch keripy", because it SUBSTITUTES one, and this
repo now decides when keripy's escrows run. The reproduction below is six lines and
``tests/test_escrows.py`` holds it to keripy's own source: a fifth escrow added upstream would
otherwise be dropped silently, and a witness that stops draining an escrow stops receipting some
class of event.
"""

from __future__ import annotations

from hio.base import doing

#: Exactly what keripy's ``WitnessStart.escrowDo`` sweeps, as ``(attribute, method)`` pairs.
#:
#: Asserted against keripy's own source rather than trusted. Changing this set without changing
#: the generator below — or the other way round — is the mistake the contract test exists to catch.
UPSTREAM_SWEEPS = frozenset(
    {
        ("kvy", "processEscrows"),
        ("rvy", "processEscrowReply"),
        ("tvy", "processEscrows"),
        ("exc", "processEscrow"),
    }
)

_DOER_NAME = "escrowDo"


def _paced(witness_start, interval):
    """keripy's escrow sweep, yielding ``interval`` instead of bare-yielding."""

    def escrowDo(tymth=None, tock=0.0, **kwa):
        witness_start.wind(tymth)
        _ = (yield tock)
        while True:
            witness_start.kvy.processEscrows()
            witness_start.rvy.processEscrowReply()
            # Guarded exactly as upstream guards it: a witness built without a TEL verifier has
            # no tvy, and sweeping None would take the whole loop down.
            if witness_start.tvy is not None:
                witness_start.tvy.processEscrows()
            witness_start.exc.processEscrow()
            yield interval

    return doing.doify(escrowDo, name=_DOER_NAME, tock=interval)


def pace(doers, interval, kind=None):
    """Pace the escrow sweep of any WitnessStart in ``doers``. Returns whether anything changed.

    ``interval`` of 0 leaves keripy exactly as it was — not "paced very fast", but untouched, so a
    deployment that wants stock behaviour runs the stock object rather than an imitation of it.

    ``kind`` exists for tests, which need a stand-in shaped like WitnessStart without building a
    real witness; production passes nothing and gets keripy's class.
    """
    if not interval:
        return False
    if kind is None:  # pragma: no cover - exercised by the launcher and the image oracles
        from keri.app.indirecting import WitnessStart

        kind = WitnessStart

    changed = False
    for doer in doers:
        if not isinstance(doer, kind):
            continue
        replacement = _paced(doer, interval)
        doer.doers = [
            replacement if getattr(sub, "__name__", None) == _DOER_NAME else sub
            for sub in doer.doers
        ]
        changed = True
    return changed
