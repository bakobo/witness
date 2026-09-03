"""Image oracle: the built image starts both processes, serves the control plane, and — the
assertion this file exists for — opens the witness LMDB as a *registered* read-only reader.

@v27j7uvo is why the last one matters. A misconfigured reader is silent: it opens, it answers,
and it returns wrong data only when the writer happens to be recycling pages underneath it. "The
image builds, starts, and answers health" passes on a broken configuration, and a measured
11,156-iteration run against a churning writer produced zero anomalies while unlocked. So the
smoke test asserts the two properties that are invisible from the outside — that the environment
is registered in the lock table, and that it is genuinely read-only — because nothing else will.

Like tests/test_smoke.py this is an end-to-end oracle and is deliberately not relied upon for
coverage. It runs only when WITNESS_IMAGE names an image to test, so it is a no-op for anyone
running the unit suite and an explicit step in CI.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

IMAGE = os.environ.get("WITNESS_IMAGE")

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set WITNESS_IMAGE to the image under test to run the image oracle"
)

_KERI_HOME = "/usr/local/var/keri"


def _docker(*args, check=True, **kwargs):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=300, **kwargs
    )


def _free_port():
    import contextlib
    import socket

    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def witness_container():
    """A running container over a freshly initialised keystore, on a loopback-published port.

    The keystore is created inside the image by `kli init` rather than on the host, because the
    image runs as uid 1001 and a host-created directory would not be writable by it. That is also
    how a real deployment initialises one, so the fixture exercises the real path.
    """
    tag = uuid.uuid4().hex[:8]
    volume = f"witness-smoke-{tag}"
    container = f"witness-smoke-{tag}"
    port = _free_port()

    _docker("volume", "create", volume)
    try:
        _docker(
            "run", "--rm", "-v", f"{volume}:{_KERI_HOME}",
            "--entrypoint", "kli", IMAGE,
            "init", "--name", "witness", "--nopasscode",
        )
        # Published to loopback only: the reverse proxy stays the sole public listener (infra
        # @m4c35y), and Docker's published-port rules bypass the INPUT chain, so binding here is
        # the confinement rather than any host firewall rule.
        _docker(
            "run", "-d", "--name", container,
            "-v", f"{volume}:{_KERI_HOME}",
            "-p", f"127.0.0.1:{port}:5633",
            IMAGE,
        )
        yield container, port
    finally:
        _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", volume, check=False)


def _await_healthz(container, port, timeout=60.0):
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _docker("inspect", "-f", "{{.State.Running}}", container, check=False)
        if state.stdout.strip() != "true":
            logs = _docker("logs", container, check=False)
            raise AssertionError(f"container exited early:\n{logs.stdout}\n{logs.stderr}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (loopback only)
                if resp.status == 200:
                    return json.loads(resp.read().decode())
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    logs = _docker("logs", container, check=False)
    raise AssertionError(f"/healthz never came up:\n{logs.stdout}\n{logs.stderr}")


def test_the_image_serves_the_control_plane(witness_container):
    container, port = witness_container

    assert _await_healthz(container, port) == {"status": "ok"}

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/info", timeout=5) as resp:  # noqa: S310
        info = json.loads(resp.read().decode())
    assert info["alias"] == "witness"
    assert info["aid"].startswith("B"), "a witness AID is non-transferable, so it starts with B"
    assert info["db_path"] == f"{_KERI_HOME}/db/witness"


def test_both_processes_run_in_the_one_container(witness_container):
    """@a24p3kbw: one container, two processes, under a supervisor that is PID 1."""
    container, port = witness_container
    _await_healthz(container, port)

    cmdlines = _docker(
        "exec", container, "sh", "-c",
        'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline; echo; done',
    ).stdout

    assert "kli witness start" in cmdlines
    assert "witness control-plane" in cmdlines
    pid1 = _docker("exec", container, "sh", "-c", 'tr "\\0" " " < /proc/1/cmdline').stdout
    assert "witness supervise" in pid1


def test_the_control_plane_reader_is_registered_and_readonly(witness_container):
    """@v27j7uvo. num_readers >= 1 proves the lock table is in use, which is what stops the
    writer recycling pages under a read transaction; readonly proves @k3p7wr's second claim."""
    container, port = witness_container
    _await_healthz(container, port)

    probe = _docker(
        "exec", container, "python", "-c",
        "from keri.db import basing; import json;"
        "b = basing.Baser(name='witness', base='', temp=False, reopen=False);"
        "b.reopen(readonly=True);"
        "print(json.dumps({'num_readers': b.env.info()['num_readers'],"
        " 'readonly': b.env.flags()['readonly']}));"
        "b.close()",
    ).stdout
    state = json.loads(probe.strip().splitlines()[-1])

    assert state["num_readers"] >= 1, (
        "the reader is not in the LMDB lock table, so the witness can recycle pages underneath "
        "it and reads can silently return wrong data"
    )
    assert state["readonly"] is True
