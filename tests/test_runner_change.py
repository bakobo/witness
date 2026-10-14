"""The runner owns the change-log writer for exactly one served process lifetime."""

import signal

import pytest

from witness import runner
from witness.config import RunnerConfig


@pytest.fixture
def config(tmp_path):
    return RunnerConfig(
        name="testwit", alias="testwit", base="", passcode=None,
        config_dir=None, config_file=None, tcp_port=5632, http_port=5631,
        telemetry_path=str(tmp_path / "telemetry"),
    )


class FakeLog:
    def __init__(self):
        self.closed_clean = False
        self.closed_unclean = False

    def close_clean(self):
        self.closed_clean = True

    def close(self):
        self.closed_unclean = True


class FakeSignals:
    SIGTERM = signal.SIGTERM
    SIGINT = signal.SIGINT

    def __init__(self):
        self.original = object()
        self.handlers = {self.SIGTERM: self.original, self.SIGINT: self.original}
        self.calls = []

    @property
    def handler(self):
        return self.handlers[self.SIGTERM]

    def restored(self):
        return all(handler is self.original for handler in self.handlers.values())

    def signal(self, signum, handler):
        assert signum in self.handlers
        self.calls.append((signum, handler))
        previous = self.handlers[signum]
        self.handlers[signum] = handler
        return previous


class FakeDoist:
    def __init__(self, *, stop_requested, **kwargs):
        self.stop_requested = stop_requested
        self.kwargs = kwargs

    def do(self):
        pass


def make_runner(config, doist_factory=FakeDoist, signals=None, events=None):
    events = [] if events is None else events
    signals = FakeSignals() if signals is None else signals
    log = FakeLog()
    db = object()
    hby = type("Hby", (), {"db": db})()

    def open_log(path):
        events.append(("log", path))
        return log

    def install(hook_db, hook_log):
        events.append(("hook", hook_db, hook_log))

    def build(_config, built_hby):
        events.append(("build", built_hby))
        return []

    subject = runner.WitnessRunner(
        config,
        open_habery=lambda _config: hby,
        build_doers=build,
        open_change_log=open_log,
        install_change_hook=install,
        change_path=lambda _config: "/change/witness",
        signal_module=signals,
        doist_factory=doist_factory,
    )
    return subject, log, signals, db, hby, events


def test_log_opens_and_hook_installs_before_doers_and_doist(config):
    events = []

    class OrderedDoist(FakeDoist):
        def do(self):
            events.append(("doist",))

    subject, log, signals, db, hby, events = make_runner(
        config, doist_factory=OrderedDoist, events=events
    )
    assert subject.run() == 0
    assert events[:3] == [
        ("log", "/change/witness"),
        ("hook", db, log),
        ("build", hby),
    ]
    assert events[3] == ("doist",)
    # A Doist that returns without a requested stop cannot vouch for the log.
    assert not log.closed_clean and log.closed_unclean
    assert signals.restored()


def test_sigterm_waits_for_the_next_doist_pass_and_closes_clean(config):
    signals = FakeSignals()

    class TerminatingDoist(FakeDoist):
        def do(self):
            assert not self.stop_requested()
            signals.handler(signals.SIGTERM, None)
            assert self.stop_requested()
            raise runner.StopRequested()

    subject, log, signals, *_ = make_runner(
        config, doist_factory=TerminatingDoist, signals=signals
    )
    assert subject.run() == 0
    assert log.closed_clean
    assert signals.restored()


def test_sigint_is_a_boundary_stop_too_not_a_keyboard_interrupt(config):
    signals = FakeSignals()

    class InterruptedDoist(FakeDoist):
        def do(self):
            signals.handlers[signals.SIGINT](signals.SIGINT, None)
            assert self.stop_requested()
            raise runner.StopRequested()

    subject, log, signals, *_ = make_runner(
        config, doist_factory=InterruptedDoist, signals=signals
    )
    assert subject.run() == 0
    assert log.closed_clean
    assert signals.restored()


@pytest.mark.parametrize(
    "failure", [RuntimeError("doist failed"), SystemExit(7), runner.StopRequested()]
)
def test_other_doist_exits_leave_log_unclean(config, failure):
    class FailingDoist(FakeDoist):
        def do(self):
            raise failure

    subject, log, signals, *_ = make_runner(config, doist_factory=FailingDoist)
    with pytest.raises(type(failure)):
        subject.run()
    assert not log.closed_clean
    assert log.closed_unclean
    assert signals.restored()


def test_stop_doist_checks_flag_before_running_another_pass(monkeypatch):
    passes = []
    monkeypatch.setattr(runner.inloop.TimedDoist, "recur", lambda self, *a, **k: passes.append(1))
    doist = runner.StopAtBoundaryDoist(
        tock=0.0, real=False, doers=[], sink=None, stop_requested=lambda: False
    )
    doist.recur()
    assert passes == [1]
    doist.stop_requested = lambda: True
    with pytest.raises(runner.StopRequested):
        doist.recur()
    assert passes == [1]
