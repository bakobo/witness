"""Supervision policy — the image runs two processes, and which one's death matters is asymmetric.

@a24p3kbw puts the runner and the control plane in one container, so something has to start both
and decide what a death means. The policy is not symmetric, and @c7v3kp is why: the control plane
is auxiliary and its absence blocks nothing, so killing a healthy witness because the auxiliary
crashed would be the exact degradation that constraint forbids. The runner is the product; when it
goes, the container goes, and the orchestrator restarts the whole thing.

Every process interaction is injected, so these tests never spawn anything. The point is the
policy, and a test that had to start real processes to prove it would be slower and less complete.
"""

import subprocess

import pytest

from witness.errors import InvalidArguments
from witness.supervisor import ProcessSpec, Supervisor


class FakeProc:
    """A Popen stand-in whose exit is scripted by the test.

    ``exits_after`` is the number of ``poll()`` calls that return None before the process reports
    ``code``. None means it never exits on its own.
    """

    def __init__(self, argv, exits_after=None, code=0, ignores_terminate=False):
        self.argv = list(argv)
        self._exits_after = exits_after
        self._code = code
        self._polls = 0
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self._ignores_terminate = ignores_terminate

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self._polls += 1
        if self._exits_after is not None and self._polls > self._exits_after:
            self.returncode = self._code
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self._ignores_terminate:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return self.returncode


class Spawner:
    """Hands out scripted FakeProcs by argv[0], recording every spawn in order."""

    def __init__(self, scripts=None):
        self._scripts = scripts or {}
        self.spawned = []

    def __call__(self, argv):
        key = argv[0]
        script = self._scripts.get(key, {})
        if isinstance(script, list):
            script = script.pop(0) if script else {}
        proc = FakeProc(argv, **script)
        self.spawned.append(proc)
        return proc


def make_supervisor(specs, spawner, **kwargs):
    """A Supervisor with time stubbed out, so no test ever actually sleeps."""
    kwargs.setdefault("sleep", lambda _seconds: None)
    return Supervisor(specs, spawn=spawner, **kwargs)


RUNNER = ProcessSpec(name="runner", argv=("kli", "witness", "start"), essential=True)
PLANE = ProcessSpec(name="control-plane", argv=("witness", "control-plane"), essential=False)


def test_run_spawns_every_configured_process():
    spawner = Spawner({"kli": {"exits_after": 1}})

    make_supervisor([RUNNER, PLANE], spawner).run()

    assert [p.argv[0] for p in spawner.spawned[:2]] == ["kli", "witness"]


def test_essential_exit_returns_its_exit_code():
    spawner = Spawner({"kli": {"exits_after": 1, "code": 3}})

    assert make_supervisor([RUNNER, PLANE], spawner).run() == 3


def test_essential_exit_terminates_the_auxiliary():
    spawner = Spawner({"kli": {"exits_after": 0}})

    make_supervisor([RUNNER, PLANE], spawner).run()

    plane = next(p for p in spawner.spawned if p.argv[0] == "witness")
    assert plane.terminated


def test_essential_process_is_not_signalled_by_its_own_death():
    """It already exited; sending it a signal would be a lie in the logs and a race in reality."""
    spawner = Spawner({"kli": {"exits_after": 0}})

    make_supervisor([RUNNER, PLANE], spawner).run()

    runner = spawner.spawned[0]
    assert not runner.terminated and not runner.killed


def test_auxiliary_death_restarts_it_and_leaves_the_witness_alone():
    """@c7v3kp: the control plane dying must not take the witness with it."""
    spawner = Spawner({
        "kli": {"exits_after": 4},
        "witness": [{"exits_after": 0}, {"exits_after": None}],
    })

    make_supervisor([RUNNER, PLANE], spawner).run()

    planes = [p for p in spawner.spawned if p.argv[0] == "witness"]
    assert len(planes) == 2, "the control plane should have been respawned"
    runner = spawner.spawned[0]
    assert not runner.terminated and not runner.killed


def test_auxiliary_restart_waits_the_restart_delay():
    slept = []
    spawner = Spawner({
        "kli": {"exits_after": 4},
        "witness": [{"exits_after": 0}, {"exits_after": None}],
    })

    Supervisor(
        [RUNNER, PLANE], spawn=spawner, sleep=slept.append, restart_delay=7.5, poll_interval=0.25
    ).run()

    assert 7.5 in slept


def test_stop_request_terminates_everything_and_exits_zero():
    spawner = Spawner()
    sup = make_supervisor([RUNNER, PLANE], spawner)
    sup.request_stop()

    assert sup.run() == 0
    assert all(p.terminated for p in spawner.spawned)


def test_stop_request_from_a_signal_handler_signature():
    """Signal handlers are called with (signum, frame); request_stop must tolerate that."""
    spawner = Spawner()
    sup = make_supervisor([RUNNER, PLANE], spawner)

    sup.request_stop(15, None)

    assert sup.run() == 0


def test_a_process_ignoring_terminate_is_killed():
    spawner = Spawner({
        "kli": {"exits_after": 0},
        "witness": {"ignores_terminate": True},
    })

    make_supervisor([RUNNER, PLANE], spawner, terminate_timeout=0.01).run()

    plane = next(p for p in spawner.spawned if p.argv[0] == "witness")
    assert plane.terminated and plane.killed


def test_spec_from_command_splits_a_shell_style_string():
    spec = ProcessSpec.from_command("runner", "kli witness start --name 'my wit'", essential=True)

    assert spec.argv == ("kli", "witness", "start", "--name", "my wit")
    assert spec.essential is True


@pytest.mark.parametrize("command", ["", "   ", "\t"])
def test_spec_from_an_empty_command_fails_closed(command):
    with pytest.raises(InvalidArguments):
        ProcessSpec.from_command("runner", command, essential=True)


def test_install_signal_handlers_routes_term_and_int_to_request_stop(monkeypatch):
    """The container's stop signal has to reach both children, so PID 1 must not ignore it."""
    import signal as signal_mod

    registered = {}
    monkeypatch.setattr(
        signal_mod, "signal", lambda sig, handler: registered.__setitem__(sig, handler)
    )
    sup = make_supervisor([RUNNER], Spawner())

    sup.install_signal_handlers()

    assert registered[signal_mod.SIGTERM] == sup.request_stop
    assert registered[signal_mod.SIGINT] == sup.request_stop
