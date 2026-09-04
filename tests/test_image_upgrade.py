"""Upgrade oracle: a replacement container over the same volume keeps the witness (~7hrf).

This is the operation that matters most the first time it is needed and is the worst one to
discover live. A witness's volume holds its AID, its KEL and every receipt it has issued; if a
new image cannot take over from an old one, the controllers that designated this witness have to
rotate, which is not a thing you find out during an incident.

So this drives the real sequence against real containers: start an image over a fresh volume, get
a controller's inception accepted and receipted, replace the container entirely, and then check
that the identity and the witnessed state survived AND that the new container still accepts a
further event. The last clause is the one that separates "the data is still on disk" from "the
witness still works", and only the second is worth anything.

Skipped unless WITNESS_IMAGE names an image, like the other oracles, and deliberately outside the
coverage gate.
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
    not IMAGE, reason="set WITNESS_IMAGE to the image under test to run the upgrade oracle"
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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _await_ready(container, port, timeout=120.0):
    """Wait until the control plane reports the loop turning, not merely until it answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _docker("inspect", "-f", "{{.State.Running}}", container, check=False)
        if state.stdout.strip() != "true":
            logs = _docker("logs", container, check=False)
            raise AssertionError(f"container exited early:\n{logs.stdout}\n{logs.stderr}")
        try:
            health = _get(port, "/v1/witness/health")
            if health.get("ticks"):
                return health
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.5)
    logs = _docker("logs", container, check=False)
    raise AssertionError(f"never became ready:\n{logs.stdout}\n{logs.stderr}")


def _start(volume, container, control_port):
    _docker(
        "run", "-d", "--name", container,
        "-v", f"{volume}:{_KERI_HOME}",
        "-p", f"127.0.0.1:{control_port}:5633",
        IMAGE,
    )
    _await_ready(container, control_port)


# A controller submitting a real, signed inception to the witness over its own HTTP port, exactly
# as a controller in the field does. Run inside the container so it uses the image's own keripy —
# which is the point, since the question is whether THAT keripy still serves THIS volume.
_SUBMIT = """
import json, sys, urllib.request
from keri.app import habbing
from keri.app.httping import CESR_CONTENT_TYPE

witness, bran, alias = sys.argv[1], sys.argv[2], sys.argv[3]
hby = habbing.Habery(name=alias, base="", temp=True, bran=bran)
hab = hby.makeHab(name=alias, transferable=True, wits=[witness], toad=1)
message = bytes(hab.msgOwnInception(framed=True))
body = hab.kever.serder.raw
request = urllib.request.Request(
    "http://127.0.0.1:5631/", data=body, method="POST",
    headers={"Content-Type": CESR_CONTENT_TYPE,
             "CESR-ATTACHMENT": message[len(body):].decode()},
)
response = urllib.request.urlopen(request, timeout=15)
print(json.dumps({"status": response.status, "controller": hab.pre}))
hby.close()
"""


def _submit_inception(container, witness_aid, bran, alias):
    """Have a controller incept with this witness designated, and return its AID."""
    result = _docker("exec", container, "python", "-c", _SUBMIT, witness_aid, bran, alias)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def volume():
    name = f"witness-upgrade-{uuid.uuid4().hex[:8]}"
    _docker("volume", "create", name)
    _docker(
        "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "--entrypoint", "kli", IMAGE,
        "init", "--name", "witness", "--nopasscode",
    )
    containers = []
    try:
        yield name, containers
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", name, check=False)


def test_a_replacement_container_keeps_the_identity_and_still_witnesses(volume):
    """The whole upgrade question in one test.

    Deliberately replaces the container rather than restarting it, because that is what deploying
    a new image digest does — `docker restart` would prove far less while looking similar.
    """
    name, containers = volume
    first, second = f"{name}-a", f"{name}-b"
    port_a, port_b = _free_port(), _free_port()

    containers.append(first)
    _start(name, first, port_a)
    witness_aid = _get(port_a, "/v1/witness/identity")["aid"]

    accepted = _submit_inception(first, witness_aid, "0987654321kjihgfedcba", "before")
    assert accepted["status"] == 204
    before_aid = accepted["controller"]
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _get(port_a, f"/v1/witness/controller/{before_aid}")
            break
        except urllib.error.HTTPError:
            time.sleep(0.5)
    before_state = _get(port_a, f"/v1/witness/controller/{before_aid}")
    assert before_state["witnesses"] == [witness_aid], "the witness should be designated"

    # Replace the container entirely, keeping only the volume — a new image digest, in effect.
    _docker("rm", "-f", first)
    containers.append(second)
    _start(name, second, port_b)

    assert _get(port_b, "/v1/witness/identity")["aid"] == witness_aid, (
        "the witness lost its AID across the replacement; every controller that designated it "
        "would have to rotate"
    )
    assert _get(port_b, f"/v1/witness/controller/{before_aid}") == before_state, (
        "the witnessed key state did not survive the replacement"
    )

    # And the half that matters: it is still a working witness, not just intact storage.
    after = _submit_inception(second, witness_aid, "mnopqrstuvw1122334455", "after")
    assert after["status"] == 204
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            fresh = _get(port_b, f"/v1/witness/controller/{after['controller']}")
            assert fresh["witnesses"] == [witness_aid]
            return
        except urllib.error.HTTPError:
            time.sleep(0.5)
    raise AssertionError("the replacement container accepted no new event")


def test_the_database_version_matches_the_keripy_the_image_carries(volume):
    """The precondition for all of the above, and the thing a pin bump changes.

    keripy records a version in the database and refuses to open one that is behind it until
    migrations run, or ahead of it at all. So a same-pin replacement works precisely because these
    agree — and the moment a keripy bump makes them disagree, the runbook in docs/deploying.md is
    what an operator needs.
    """
    name, containers = volume
    container = f"{name}-v"
    port = _free_port()
    containers.append(container)
    _start(name, container, port)

    probe = _docker(
        "exec", container, "python", "-c",
        "import json, keri; from keri.db import basing;"
        "b = basing.Baser(name='witness', base='', temp=False, reopen=False);"
        "b.reopen(readonly=True);"
        "print(json.dumps({'db': b.version, 'library': keri.__version__, 'current': b.current}));"
        "b.close()",
    ).stdout
    state = json.loads(probe.strip().splitlines()[-1])

    assert state["db"] == state["library"]
    assert state["current"] is True
