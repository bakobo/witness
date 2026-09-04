"""Process vitals for the co-located witness runner, read from /proc.

Available only because @a24p3kbw put both processes in one container and therefore one PID
namespace — from a separate container the runner's /proc would not be visible at all. This is one
of the signals that stopped needing a hook the moment they became co-located.

ops.md §7 says a third-party process we merely run gets host metrics and black-box probes,
"because they expose nothing else". This is the host-metrics half of that, scoped to the one
process that matters rather than the whole box.
"""

from __future__ import annotations

import os

from .errors import RunnerNotRunning

_RUNNER_MARKERS = ("witness run", "kli witness start")
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _cmdline(pid, proc="/proc"):
    try:
        with open(f"{proc}/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return None  # the process exited between listing and reading, which is not an error


def find_runner_pid(proc="/proc"):
    """The pid of the witness runner, or ``None``.

    Matched on the command line rather than on a pid file, because the supervisor already knows
    how to start the runner and a pid file would be a second source of truth that can go stale
    while the process it names is gone.
    """
    for entry in sorted(os.listdir(proc)):
        if not entry.isdigit():
            continue
        command = _cmdline(entry, proc=proc)
        if command and any(marker in command for marker in _RUNNER_MARKERS):
            if "supervise" in command:
                continue  # that is the supervisor's own command line, which names both children
            return int(entry)
    return None


def runner_vitals(proc="/proc"):
    """CPU, memory and descriptor counts for the running witness."""
    pid = find_runner_pid(proc=proc)
    if pid is None:
        raise RunnerNotRunning(
            "The witness runner process is not running, so its vitals cannot be read."
        )
    try:
        with open(f"{proc}/{pid}/stat") as handle:
            fields = handle.read().rsplit(") ", 1)[1].split()
        with open(f"{proc}/{pid}/status") as handle:
            status = dict(
                line.split(":", 1) for line in handle.read().splitlines() if ":" in line
            )
        descriptors = len(os.listdir(f"{proc}/{pid}/fd"))
    except OSError as exc:
        raise RunnerNotRunning(
            "The witness runner process went away while its vitals were being read."
        ) from exc

    # /proc/<pid>/stat fields, after the comm field: state is 0, utime 11, stime 12, rss 21.
    return {
        "pid": pid,
        "state": fields[0],
        "cpu_seconds": (int(fields[11]) + int(fields[12])) / _CLOCK_TICKS,
        "threads": int(fields[17]),
        "resident_bytes": int(fields[21]) * _PAGE_SIZE,
        "open_descriptors": descriptors,
        "vm_rss_bytes": _kb(status.get("VmRSS")),
    }


def _kb(value):
    """Parse a `/proc/<pid>/status` "N kB" field into bytes, or None when it is absent."""
    if value is None:  # a kernel or a container runtime that omits VmRSS
        return None
    return int(value.strip().split()[0]) * 1024
