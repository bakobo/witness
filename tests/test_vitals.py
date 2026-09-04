"""Process vitals for the co-located runner, read from a fake /proc.

Reachable at all only because @a24p3kbw put both processes in one container and therefore one PID
namespace — from a separate container the runner's /proc would be invisible. Tested against a
synthetic tree rather than the live one so the assertions are about the parsing, which is where
the mistakes live: /proc/<pid>/stat's fields are positional, and the process name sits in
parentheses that may themselves contain spaces or parentheses.
"""

import os

import pytest

from witness import vitals
from witness.errors import RunnerNotRunning


def make_proc(root, pid, cmdline, *, comm="python", utime=200, stime=100, threads=3, rss=4096):
    """A minimal /proc/<pid> that the parser will accept."""
    directory = root / str(pid)
    (directory / "fd").mkdir(parents=True)
    (directory / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")
    # stat: pid (comm) state, then the positional fields the parser indexes after "') '".
    # /proc/<pid>/stat carries ~52 fields; 26 is enough to reach the ones the parser indexes.
    fields = ["S"] + ["0"] * 25
    fields[11] = str(utime)
    fields[12] = str(stime)
    fields[17] = str(threads)
    fields[21] = str(rss // (os.sysconf("SC_PAGE_SIZE")))
    (directory / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    (directory / "status").write_text(f"Name:\t{comm}\nVmRSS:\t   64 kB\n")
    for descriptor in range(4):
        (directory / "fd" / str(descriptor)).write_text("")
    return directory


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "self").mkdir()  # a non-numeric entry, which the scan must skip
    return root


def test_the_runner_is_found_by_its_command_line(proc):
    make_proc(proc, 1, "witness supervise --essential witness run --auxiliary x")
    make_proc(proc, 8, "witness run --name witness --alias witness")
    make_proc(proc, 9, "witness control-plane --name witness")

    assert vitals.find_runner_pid(proc=str(proc)) == 8


def test_the_supervisor_is_not_mistaken_for_the_runner(proc):
    """PID 1's command line names both children, so a naive substring match finds it first and
    reports the supervisor's vitals as the witness's — which look fine no matter what the witness
    is doing."""
    make_proc(proc, 1, "witness supervise --essential witness run --auxiliary y")

    assert vitals.find_runner_pid(proc=str(proc)) is None


def test_a_stock_kli_runner_is_recognised_too(proc):
    """The image runs `witness run`, but a witness started the old way is still a witness."""
    make_proc(proc, 12, "kli witness start --name witness --alias witness")

    assert vitals.find_runner_pid(proc=str(proc)) == 12


def test_a_process_that_exits_mid_scan_is_skipped_rather_than_fatal(proc, monkeypatch):
    make_proc(proc, 8, "witness run --name witness")
    real_open = open

    def vanishing_open(path, *args, **kwargs):
        if str(path).endswith("/8/cmdline"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", vanishing_open)

    assert vitals.find_runner_pid(proc=str(proc)) is None


def test_vitals_report_cpu_memory_threads_and_descriptors(proc):
    make_proc(proc, 8, "witness run --name witness", utime=200, stime=100, threads=5)

    reported = vitals.runner_vitals(proc=str(proc))

    assert reported["pid"] == 8
    assert reported["state"] == "S"
    assert reported["cpu_seconds"] == pytest.approx(300 / os.sysconf("SC_CLK_TCK"))
    assert reported["threads"] == 5
    assert reported["open_descriptors"] == 4
    assert reported["vm_rss_bytes"] == 64 * 1024


def test_a_command_name_containing_spaces_does_not_shift_the_fields(proc):
    """/proc/<pid>/stat puts the executable name in parentheses and does not escape what is
    inside them, so splitting on whitespace mis-indexes every field after it. Splitting on the
    last ') ' is what makes this safe, and this is the test that says so."""
    make_proc(proc, 8, "witness run --name witness", comm="py (thon) x", utime=400, stime=0)

    assert vitals.runner_vitals(proc=str(proc))["cpu_seconds"] == pytest.approx(
        400 / os.sysconf("SC_CLK_TCK")
    )


def test_vitals_for_a_witness_that_is_not_running_fail_closed(proc):
    with pytest.raises(RunnerNotRunning) as caught:
        vitals.runner_vitals(proc=str(proc))

    assert caught.value.retryable is True


def test_a_runner_that_exits_between_being_found_and_being_read(proc, monkeypatch):
    """The race is real: the scan finds a pid, the process dies, and the read fails. Reporting
    that as transient is honest — the supervisor is about to restart it or the container is going
    down, and either way retrying answers the question."""
    make_proc(proc, 8, "witness run --name witness")
    monkeypatch.setattr(vitals, "find_runner_pid", lambda proc=None: 999999)

    with pytest.raises(RunnerNotRunning):
        vitals.runner_vitals(proc=str(proc))


def test_a_kernel_that_omits_vmrss_reports_none_rather_than_crashing(proc):
    """TST-F4: this branch was behind a pragma. Not every runtime exposes VmRSS, and losing one
    field should not lose the whole vitals response."""
    directory = make_proc(proc, 8, "witness run --name witness")
    (directory / "status").write_text("Name:\tpython\n")

    reported = vitals.runner_vitals(proc=str(proc))

    assert reported["vm_rss_bytes"] is None
    assert reported["pid"] == 8


def test_resident_bytes_is_reported_from_the_stat_rss_field(proc):
    """TST-F5: the field was returned but never asserted, so a wrong index was invisible."""
    page = os.sysconf("SC_PAGE_SIZE")
    make_proc(proc, 8, "witness run --name witness", rss=page * 7)

    assert vitals.runner_vitals(proc=str(proc))["resident_bytes"] == page * 7
