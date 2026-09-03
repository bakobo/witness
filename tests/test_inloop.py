"""Code that runs on the witness's own Doist thread — the timing wrapper, the telemetry doer, and
the test that freezes this module's hot path (@vpu373to, @e3uji3mv).

The transparency tests exist because a wrapper sitting between hio and every keripy doer is the
highest-blast-radius code in this repo. hio drives dogs with ``send``, reads the yielded tock,
assigns ``doer.done`` from ``dog.close()``'s return value, and relies on ``StopIteration.value``.
Each of those is asserted here against a wrapped generator and its bare equivalent, because
"looks transparent" is not a property.

``test_the_hot_path_stays_frozen`` is the enforcement @e3uji3mv describes. It is meant to be
annoying: widening the allowlist takes a deliberate commit that says why.
"""

import ast
import pathlib

import pytest

from witness import inloop, telemetry


def drive(gen, values):
    """Send each value into a primed generator, collecting what it yields."""
    return [gen.send(value) for value in values]


class RecordingSink:
    def __init__(self):
        self.events = []

    def mark_enter(self, slot, now=None):
        self.events.append(("enter", slot))

    def mark_exit(self, slot, now=None):
        self.events.append(("exit", slot))


def make_dog(values=("t1", "t2"), returns="DONE"):
    def dog():
        try:
            for value in values:
                yield value
            return returns
        except GeneratorExit:
            return returns

    generator = dog()
    next(generator)
    return generator


def test_the_wrapper_passes_yielded_tocks_through():
    sink = RecordingSink()
    wrapped = inloop.timed(make_dog(values=("a", "b", "c")), slot=0, sink=sink)
    next(wrapped)

    assert drive(wrapped, [1.0, 2.0]) == ["b", "c"]


def test_the_wrapper_propagates_the_return_value_as_hio_reads_it():
    """hio takes doer.done from StopIteration.value, so losing it silently marks a doer
    incomplete forever."""
    wrapped = inloop.timed(make_dog(values=("a",), returns="FIN"), slot=0, sink=RecordingSink())
    next(wrapped)

    with pytest.raises(StopIteration) as excinfo:
        wrapped.send(1.0)

    assert excinfo.value.value == "FIN"


def test_close_returns_what_the_inner_generator_returns():
    """Doist.exit assigns doer.done from dog.close(); a wrapper that swallows it breaks shutdown."""
    bare = make_dog(returns="CLOSED")
    wrapped = inloop.timed(make_dog(returns="CLOSED"), slot=0, sink=RecordingSink())
    next(wrapped)

    assert wrapped.close() == bare.close() == "CLOSED"


def test_an_exception_inside_a_doer_propagates_unchanged():
    def exploding():
        yield "first"
        raise ValueError("doer blew up")

    generator = exploding()
    next(generator)
    wrapped = inloop.timed(generator, slot=0, sink=RecordingSink())
    next(wrapped)

    with pytest.raises(ValueError, match="doer blew up"):
        wrapped.send(1.0)


def test_the_slot_is_marked_before_the_doer_runs_not_after():
    """The whole forensic value: a doer that never returns has already been recorded as current."""
    sink = RecordingSink()

    def hangs():
        yield "first"
        sink.events.append(("inside", 3))
        yield "second"

    generator = hangs()
    next(generator)
    wrapped = inloop.timed(generator, slot=3, sink=sink)
    next(wrapped)
    wrapped.send(1.0)

    assert sink.events == [("enter", 3), ("inside", 3), ("exit", 3)]


def test_an_exception_still_closes_the_timing_span():
    def exploding():
        yield "first"
        raise ValueError("boom")

    generator = exploding()
    next(generator)
    sink = RecordingSink()
    wrapped = inloop.timed(generator, slot=2, sink=sink)
    next(wrapped)

    with pytest.raises(ValueError):
        wrapped.send(1.0)

    assert sink.events[-1] == ("exit", 2)


def test_timed_doist_wraps_every_dog_and_leaves_the_doers_untouched(tmp_path):
    """@vpu373to: doer identity, .tock, .done and .opts must survive, because hio reads and
    writes all of them — including through doer.__func__ for bound-method generators."""
    from hio.base import doing

    class Counter(doing.Doer):
        def __init__(self, **kwa):
            super().__init__(**kwa)
            self.passes = 0

        def recur(self, tyme):
            self.passes += 1
            return False

    doers = [Counter(tock=0.0), Counter(tock=0.0)]
    doist = inloop.TimedDoist(tock=0.03125, real=False, doers=doers, sink=RecordingSink())

    doist.enter()
    for _ in range(3):
        doist.recur()

    assert all(doer.passes == 3 for doer in doers)
    assert [d for d in doist.doers] == doers, "doer objects must not be replaced"
    assert all(doer.done is False for doer in doers)


def test_the_telemetry_doer_publishes_ticks_and_lag(tmp_path):
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["only"])
    clock = iter([100.0, 100.5, 101.0])
    try:
        doer = inloop.TelemetryDoer(writer=writer, tock=0.25, monotonic=lambda: next(clock))
        doer.publish()
        doer.publish()

        state = telemetry.SegmentReader(str(path)).read()
    finally:
        writer.close()

    assert state["ticks"] == 2
    # The pass took 0.5s against a 0.25s tock, so the loop is running 0.25s behind.
    assert state["loop_lag"] == pytest.approx(0.25)


def test_lag_is_never_negative_when_the_loop_is_keeping_up(tmp_path):
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["only"])
    clock = iter([10.0, 10.01])
    try:
        doer = inloop.TelemetryDoer(writer=writer, tock=1.0, monotonic=lambda: next(clock))
        doer.publish()
        doer.publish()
        state = telemetry.SegmentReader(str(path)).read()
    finally:
        writer.close()

    assert state["loop_lag"] == 0.0


# --------------------------------------------------------------------------------------------
# The freeze (@e3uji3mv)
# --------------------------------------------------------------------------------------------

EXPECTED_HOT_PATH = {
    "timed", "publish", "mark_enter", "mark_exit", "publish_tick",
    "_begin_write", "_end_write",
}

# Every name a function on the witness's loop thread may call. Deliberately short. `send` and
# `close` are generator protocol, `pack_into` writes into an already-mapped buffer, and the rest
# are our own hot-path helpers, which carry @hot_path themselves and so are frozen too.
ALLOWED_CALLS = {
    "pack_into", "max", "min",
    "send", "close",
    # time.monotonic is a vDSO read on Linux — no syscall, no allocation of consequence — and
    # measuring elapsed time is the one thing a timing wrapper cannot do without.
    "monotonic", "_monotonic",
    "_begin_write", "_end_write", "publish_tick", "mark_enter", "mark_exit",
}


def _hot_path_functions(tree):
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                name = decorator.id if isinstance(decorator, ast.Name) else None
                if name == "hot_path":
                    found[node.name] = node
    return found


def test_the_hot_path_stays_frozen():
    """@e3uji3mv. This test is the containment, and it is meant to be annoying.

    The risk is not one careless commit. It is "just one more small doer", repeated, until the
    witness process is full of Bakobo code on its hot path and the separate-process guarantee
    @c7v3kp bought has quietly evaporated. Widening ALLOWED_CALLS should cost a commit that
    explains itself.
    """
    for module in (inloop, telemetry):
        source = pathlib.Path(module.__file__).read_text()
        tree = ast.parse(source)
        for name, node in _hot_path_functions(tree).items():
            for child in ast.walk(node):
                assert not isinstance(child, (ast.Import, ast.ImportFrom)), (
                    f"{name} imports at call time; imports belong at module scope"
                )
                assert not isinstance(child, (ast.With, ast.AsyncWith)), (
                    f"{name} uses a context manager, which nearly always means acquiring a "
                    f"resource — the generator protocol needs try/except, but not `with`"
                )
                if isinstance(child, ast.Call):
                    func = child.func
                    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    assert called in ALLOWED_CALLS, (
                        f"{name} calls {called!r}, which is not in the hot-path allowlist. "
                        f"If it truly belongs on the witness's loop thread, widen ALLOWED_CALLS "
                        f"in a commit that says why (@e3uji3mv)."
                    )


def test_every_expected_hot_path_function_is_still_marked():
    """Removing @hot_path would silently exempt a function from the freeze above, so the set of
    marked functions is itself asserted."""
    marked = set()
    for module in (inloop, telemetry):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        marked |= set(_hot_path_functions(tree))

    assert marked == EXPECTED_HOT_PATH


def test_slots_survive_a_doer_that_finishes_during_enter():
    """Doist.enter skips any doer that completes in its own enter, so assigning slots from the
    deeds would shift every later doer's name by one and mislabel the timings silently."""
    from hio.base import doing

    class Instant(doing.Doer):
        def do(self, tymth, *, tock=0.0, temp=None, **opts):
            return True
            yield  # pragma: no cover - unreachable, but makes this a generator function

    class Lasting(doing.Doer):
        def recur(self, tyme):
            return False

    doers = [Instant(tock=0.0), Lasting(tock=0.0)]
    sink = RecordingSink()
    doist = inloop.TimedDoist(tock=0.03125, real=False, doers=doers, sink=sink)

    doist.enter()
    doist.recur()

    assert inloop.TimedDoist.names_for(doers) == ["Instant", "Lasting"]
    # Only Lasting is still running, and it must be timed as slot 1 — its position in doers —
    # not as slot 0, which is what deed-order assignment would have given it.
    assert {slot for _event, slot in sink.events} == {1}


def test_a_doer_beyond_the_segments_capacity_is_left_untimed():
    """Better to lose one doer's timings than to write past the end of the mapping."""
    from hio.base import doing

    class Plain(doing.Doer):
        def recur(self, tyme):
            return False

    doers = [Plain(tock=0.0), Plain(tock=0.0)]
    sink = RecordingSink()
    doist = inloop.TimedDoist(tock=0.03125, real=False, doers=doers, sink=sink, capacity=1)

    doist.enter()
    doist.recur()

    assert {slot for _event, slot in sink.events} == {0}


def test_a_doer_added_after_enter_gets_its_own_slot():
    """hio's Doist.extend admits doers mid-run; they must be named, not silently share a slot."""
    from hio.base import doing

    class Late(doing.Doer):
        def recur(self, tyme):
            return False

    doist = inloop.TimedDoist(tock=0.03125, real=False, doers=[], sink=RecordingSink(), capacity=4)
    doist.enter()
    doist.extend([Late(tock=0.0)])

    assert doist.names_for(doist.doers) == ["Late"]
    assert doist._slot_for(doist.doers[0]) == 0


def test_the_telemetry_doer_publishes_once_per_recur(tmp_path):
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["only"])
    try:
        doer = inloop.TelemetryDoer(writer=writer, tock=0.25)
        assert doer.recur(0.0) is False
        doer.recur(0.0)
        assert telemetry.SegmentReader(str(path)).read()["ticks"] == 2
    finally:
        writer.close()


def test_names_disambiguate_duplicates_and_bound_method_doers():
    """A real witness runs two ServerDoers and several bound-method doers. Naming them all
    'ServerDoer' and 'method' would point a wedge investigation at nothing."""
    from hio.base import doing

    class ServerDoer(doing.Doer):
        pass

    class Holder:
        def escrowDo(self, tymth, tock=0.0, **opts):
            yield  # pragma: no cover - never driven here

    holder = Holder()
    names = inloop.TimedDoist.names_for(
        [ServerDoer(), ServerDoer(), ServerDoer(), holder.escrowDo]
    )

    assert names == ["ServerDoer", "ServerDoer#2", "ServerDoer#3", "Holder.escrowDo"]
