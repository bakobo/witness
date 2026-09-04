"""Code that executes on the witness's own hio Doist thread.

This module is the one place Bakobo code runs inside the witness process, and @e3uji3mv holds it
to publishing: no I/O, no syscalls, no LMDB, no unbounded iteration, no branching on anything a
remote party controls. ``tests/test_inloop.py::test_the_hot_path_stays_frozen`` parses this file
and fails on any call outside a short allowlist. That test is the containment; the docstring is
only its explanation.

The reason for the ceremony is not one careless commit. It is that "just one more small doer" is
how a process ends up hosting a control plane nobody decided to put there — and the whole
argument for the separate-process design (@c7v3kp, @k3p7wr) is that it *cannot* stall the
witness. That guarantee is structural everywhere except here, so here it is a test.
"""

from __future__ import annotations

import time

from hio.base import doing

from .telemetry import hot_path


@hot_path
def timed(dog, slot, sink):
    """Wrap one hio ``dog`` generator so its run time lands in the telemetry segment.

    Transparency is the whole requirement, because hio depends on four separate behaviours of the
    generator it is handed (@vpu373to). ``send`` must return the yielded tock. ``StopIteration``
    must carry the doer's return value, which ``Doist.enter`` and ``recur`` assign to
    ``doer.done``. ``close()`` must return the inner generator's value, which ``Doist.exit``
    assigns the same way. And any other exception must arrive unchanged. All four are asserted
    against a bare generator in the tests.

    ``StopIteration`` is caught and converted to a ``return`` rather than re-raised, because
    PEP 479 turns a ``StopIteration`` raised inside a generator body into a ``RuntimeError`` —
    re-raising it would corrupt the very signal hio reads.

    The slot is marked before the send and after it, so a doer that never returns has already
    recorded itself as the one executing. That is the forensic payload: a wedged witness names
    its own culprit, where a duration-only scheme would record nothing at all.
    """
    try:
        # The priming yield is INSIDE the try on purpose: a Doist that exits before this deed has
        # run even once still closes it, and a GeneratorExit arriving here must still reach the
        # inner dog. With the yield outside, close() returned None and the doer's done state was
        # silently lost.
        sent = yield  # the caller discards this first value, as hio discards enter's
        while True:
            sink.mark_enter(slot, now=time.monotonic())
            try:
                tock = dog.send(sent)
            except StopIteration as stop:
                sink.mark_exit(slot, now=time.monotonic())
                return stop.value
            except BaseException:
                sink.mark_exit(slot, now=time.monotonic())
                raise
            sink.mark_exit(slot, now=time.monotonic())
            sent = yield tock
    except GeneratorExit:
        return dog.close()


class TimedDoist(doing.Doist):
    """A Doist that times each doer without touching any doer.

    Overrides ``enter`` alone. hio's ``enter`` builds the deeds deque of ``(dog, retyme, doer)``
    triples; this replaces each ``dog`` with :func:`timed` and leaves the ``doer`` object exactly
    as it was — same identity, same ``tock``, ``done``, ``opts`` and ``temp``. Nothing of hio's
    scheduling is reproduced here, so a keripy or hio bump has three lines to break rather than
    forty (@vpu373to).
    """

    def __init__(self, *args, sink=None, capacity=None, **kwargs):
        self.sink = sink
        super().__init__(*args, **kwargs)
        # Slots are assigned from self.doers up front, not as deeds are built, because
        # Doist.enter SKIPS any doer that completes during its own enter — assigning slots from
        # the deeds would then shift every later doer's name by one and silently mislabel the
        # timings, which is worse than having none.
        self._names = TimedDoist.names_for(self.doers)
        self._slots = {id(doer): index for index, doer in enumerate(self.doers)}
        self._capacity = len(self._names) if capacity is None else capacity

    @staticmethod
    def names_for(doers):
        """The labels a segment must be created with, in slot order, for ``doers``.

        Two things a naive ``type(doer).__name__`` gets wrong against a real witness. hio accepts
        bound-method generators as doers, and every one of those is called ``method``, which names
        nothing. And keripy runs two ``ServerDoer`` instances — the HTTP one and the TCP one —
        which a bare class name cannot tell apart, so a wedge would point at both. Qualified names
        fix the first and an ordinal suffix fixes the second.
        """
        names = []
        for doer in doers:
            qualified = getattr(doer, "__qualname__", None) or type(doer).__name__
            # Keep the last two meaningful segments: "Oobiery.escrowDo" rather than a full
            # dotted path, which would not fit the segment's 32-byte name field anyway.
            parts = [part for part in qualified.split(".") if part != "<locals>"]
            base = ".".join(parts[-2:])
            if base in names:
                base = f"{base}#{sum(1 for n in names if n.split('#')[0] == base) + 1}"
            names.append(base)
        return names

    def enter(self, doers=None, *, temp=None):
        deeds = super().enter(doers=doers, temp=temp)
        if self.sink is None:  # a Doist built without telemetry times nothing
            return deeds
        wrapped = [self._wrap(deed) for deed in deeds]
        deeds.clear()
        deeds.extend(wrapped)
        return deeds

    def _wrap(self, deed):
        dog, retyme, doer = deed
        slot = self._slot_for(doer)
        if slot is None:
            return deed  # more doers than the segment holds: no timings rather than a bad write
        wrapper = timed(dog, slot, self.sink)
        next(wrapper)  # prime it; hio has already advanced the inner dog past its enter yield
        return (wrapper, retyme, doer)

    def _slot_for(self, doer):
        key = id(doer)
        if key not in self._slots:  # a doer added later via Doist.extend
            self._slots[key] = len(self._names)
            self._names.append(TimedDoist.names_for([doer])[0])
        slot = self._slots[key]
        return slot if slot < self._capacity else None


class TelemetryDoer(doing.Doer):
    """One cheap doer that records that the loop is still turning, and how far behind it is.

    Loop lag is what infra asked to alert on: it measures the harm in a single-threaded
    cooperative loop directly, where CPU and memory thresholds measure nothing actionable.
    """

    def __init__(self, writer, tock=1.0, monotonic=time.monotonic, **kwa):
        super().__init__(tock=tock, **kwa)
        self._writer = writer
        self._monotonic = monotonic
        self._ticks = 0
        self._last = None

    @hot_path
    def publish(self):
        """Record one pass. Lag is how much longer than a tock the previous pass took."""
        now = self._monotonic()
        lag = 0.0
        if self._last is not None:
            lag = max(0.0, now - self._last - self.tock)
        self._last = now
        self._ticks = self._ticks + 1
        self._writer.publish_tick(ticks=self._ticks, wall=now, lag=lag)

    def recur(self, tyme):
        self.publish()
        return False
