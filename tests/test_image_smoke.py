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
    url = f"http://127.0.0.1:{port}/v1/witness/health"
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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def test_the_image_serves_the_control_plane(witness_container):
    container, port = witness_container

    health = _await_healthz(container, port)
    assert health["status"] == "ok"
    assert health["ticks"] > 0, "health should see the loop ticking, not merely the DB opening"

    identity = _get(port, "/v1/witness/identity")
    assert identity["alias"] == "witness"
    assert identity["aid"].startswith("B"), "a witness AID is non-transferable, so it starts with B"


def test_the_whole_versioned_surface_answers_against_a_real_witness(witness_container):
    """Every Phase 2 view against a live witness rather than a stub — the shapes are only worth
    anything if keripy's real accessors produce them."""
    container, port = witness_container
    _await_healthz(container, port)

    assert _get(port, "/v1/witness/version")["keripy"].startswith("2.")
    assert "query_not_found" in _get(port, "/v1/witness/escrow")["depths"]

    database = _get(port, "/v1/witness/database")
    assert database["path"] == f"{_KERI_HOME}/db/witness"
    assert 0 <= database["used_fraction"] <= 1
    assert database["readers"] >= 1

    process = _get(port, "/v1/witness/process")
    assert process["pid"] > 1, "pid 1 is the supervisor, not the witness"
    assert process["threads"] >= 1

    loop = _get(port, "/v1/witness/loop")
    assert loop["ticks"] > 0
    assert any(doer["name"] == "HaberyDoer" for doer in loop["doers"])

    controllers = _get(port, "/v1/witness/controller")["controllers"]
    assert all(set(entry) == {"aid", "sequence_number", "said"} for entry in controllers)


def test_an_unknown_controller_is_a_404_problem_document(witness_container):
    """The envelope has to survive the round trip through waitress, not just falcon's test client."""
    container, port = witness_container
    _await_healthz(container, port)

    request = urllib.request.Request(  # noqa: S310
        f"http://127.0.0.1:{port}/v1/witness/controller/EAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    try:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310
        raise AssertionError("an unwitnessed AID should not be found")
    except urllib.error.HTTPError as failure:
        assert failure.code == 404
        assert failure.headers["content-type"].startswith("application/problem+json")
        body = json.loads(failure.read().decode())

    assert body["code"] == "e.state.missing.controller.f"
    assert body["type"] == "https://errors.bakobo.com/e.state.missing.controller.f"
    assert body["instance"].startswith("/v1/witness/controller/")
    assert body["request_id"]


def test_both_processes_run_in_the_one_container(witness_container):
    """@a24p3kbw: one container, two processes, under a supervisor that is PID 1."""
    container, port = witness_container
    _await_healthz(container, port)

    cmdlines = _docker(
        "exec", container, "sh", "-c",
        'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline; echo; done',
    ).stdout

    assert "witness run" in cmdlines
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


def test_the_running_witness_publishes_telemetry(witness_container):
    """@vxt7feoi and ~2x3n. The segment has to exist, name the real keripy doers, and show the
    loop turning — a witness that publishes a stuck tick count is exactly the wedge this is for."""
    container, port = witness_container
    _await_healthz(container, port)

    def snapshot():
        probe = _docker(
            "exec", container, "python", "-c",
            "import json; from witness.telemetry import SegmentReader;"
            f"print(json.dumps(SegmentReader('{_KERI_HOME}/telemetry').read()))",
        ).stdout
        return json.loads(probe.strip().splitlines()[-1])

    first = snapshot()
    names = [doer["name"] for doer in first["doers"]]

    assert "TelemetryDoer" in names
    assert "HaberyDoer" in names, f"the stock keripy doers should be named, got {names}"
    assert first["ticks"] > 0

    time.sleep(2)
    assert snapshot()["ticks"] > first["ticks"], "the loop is not turning"


def test_the_telemetry_segment_is_not_writable_through_the_reader(witness_container):
    """The kernel-enforced read-only mapping @a24p3kbw could not get for LMDB, which needs a
    writable lock file. A purpose-built segment has no lock protocol, so here it is real."""
    container, port = witness_container
    _await_healthz(container, port)

    result = _docker(
        "exec", container, "python", "-c",
        "from witness.telemetry import SegmentReader;"
        f"r = SegmentReader('{_KERI_HOME}/telemetry');"
        "\ntry:\n    r._map[0:1] = b'x'\n    print('WRITABLE')\nexcept (TypeError, OSError) as e:\n"
        "    print('refused:', type(e).__name__)",
    ).stdout

    assert "refused" in result, result
