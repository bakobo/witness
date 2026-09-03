"""Supervision for the two co-located processes in the witness image.

@a24p3kbw puts the keripy runner and the control plane in one container, so something inside the
image has to start both and decide what each one's death means. The policy is deliberately
asymmetric, and @c7v3kp is the reason: the control plane is auxiliary — its absence blocks
nothing — so taking a healthy witness down because the auxiliary crashed would be exactly the
degradation that constraint forbids. The runner is the product. When it exits, the container
exits with its code and the orchestrator restarts the whole thing.

Written here rather than delegated to supervisord or s6 because the org standard is provable
100% branch coverage, and an untested shell entrypoint or an opaque third-party binary would be
a hole in it. The policy above is also not what either gives you by default. Every process
interaction is injected, so the policy is testable without spawning anything.

Reaping is limited to our own children, which ``Popen.poll`` handles. Neither keripy's hio loop
nor waitress forks, so no process is orphaned onto us; if that ever changes, this module is where
a ``waitpid(-1)`` reaper belongs.
"""

from __future__ import annotations

import shlex
import signal
import subprocess
import time
from dataclasses import dataclass

from .errors import InvalidArguments

_POLL_INTERVAL = 0.25
_RESTART_DELAY = 1.0
_TERMINATE_TIMEOUT = 10.0


@dataclass(frozen=True)
class ProcessSpec:
    """One supervised process: what to run, and whether the container depends on it."""

    name: str
    argv: tuple[str, ...]
    essential: bool

    @classmethod
    def from_command(cls, name: str, command: str, *, essential: bool) -> ProcessSpec:
        """Build a spec from a shell-style command string, failing closed on an empty one.

        Commands arrive as single strings because the image supplies them as CMD arguments, and
        keeping them opaque here is what lets the runner switch from ``kli witness start`` to
        ``witness run`` without touching this module.
        """
        argv = shlex.split(command)
        if not argv:
            raise InvalidArguments(
                f"The {name} command is empty; it must name a program to run."
            )
        return cls(name=name, argv=tuple(argv), essential=essential)


@dataclass
class _Running:
    spec: ProcessSpec
    proc: object


class Supervisor:
    """Runs a set of :class:`ProcessSpec` until the essential one exits or a stop is requested."""

    def __init__(
        self,
        specs,
        *,
        spawn=subprocess.Popen,
        sleep=time.sleep,
        poll_interval=_POLL_INTERVAL,
        restart_delay=_RESTART_DELAY,
        terminate_timeout=_TERMINATE_TIMEOUT,
    ):
        self._specs = list(specs)
        self._spawn = spawn
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._restart_delay = restart_delay
        self._terminate_timeout = terminate_timeout
        self._running: dict[str, _Running] = {}
        self._stopping = False

    def install_signal_handlers(self):
        """Route SIGTERM and SIGINT to :meth:`request_stop`.

        As PID 1 the supervisor receives the container's stop signal, and a PID 1 that ignores it
        turns every graceful stop into a ten-second wait followed by SIGKILL — which is how a
        witness loses whatever its write transaction was holding.
        """
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def request_stop(self, *_signal_args):
        """Ask the supervision loop to shut everything down. Safe as a signal handler."""
        self._stopping = True

    def run(self) -> int:
        """Supervise until shutdown; return the exit code the container should carry."""
        for spec in self._specs:
            self._start(spec)
        while True:
            if self._stopping:
                self._shutdown()
                return 0
            for name, entry in list(self._running.items()):
                code = entry.proc.poll()
                if code is None:
                    continue
                if entry.spec.essential:
                    # It is already gone; signalling it would be a lie in the logs and a race.
                    self._shutdown(except_name=name)
                    return code
                self._sleep(self._restart_delay)
                self._start(entry.spec)
            self._sleep(self._poll_interval)

    def _start(self, spec: ProcessSpec):
        self._running[spec.name] = _Running(spec=spec, proc=self._spawn(list(spec.argv)))

    def _shutdown(self, except_name=None):
        for name, entry in self._running.items():
            if name == except_name:
                continue
            entry.proc.terminate()
            try:
                entry.proc.wait(timeout=self._terminate_timeout)
            except subprocess.TimeoutExpired:
                entry.proc.kill()
                entry.proc.wait()
