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

# The image this one is upgrading FROM -- in practice the digest production is running (@ktljhcyt).
# Absent rather than defaulted to IMAGE, deliberately: falling back would make a run with no
# baseline configured produce output indistinguishable from a real two-version handover, which is
# the weaker claim wearing the stronger one's clothes. Unset means the handover tests SKIP and say
# which variable would have run them.
BASELINE = os.environ.get("WITNESS_BASELINE_IMAGE")

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set WITNESS_IMAGE to the image under test to run the upgrade oracle"
)

needs_baseline = pytest.mark.skipif(
    not BASELINE,
    reason="set WITNESS_BASELINE_IMAGE to the image being upgraded FROM (the deployed digest) "
    "to run the two-version handover",
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


def _start(volume, container, control_port, image=None):
    _docker(
        "run", "-d", "--name", container,
        "-v", f"{volume}:{_KERI_HOME}",
        "-p", f"127.0.0.1:{control_port}:5633",
        image or IMAGE,
    )
    _await_ready(container, control_port)


def _keripy_version(image):
    """The keripy an image carries, read from the image rather than from a label.

    A label would be this repo asserting the version; this asks the interpreter that will actually
    open the database. The two agree today and the difference is the whole question when a pin
    moves, which is when this matters.
    """
    result = _docker(
        "run", "--rm", "--entrypoint", "python", image, "-c", "import keri; print(keri.__version__)"
    )
    return result.stdout.strip().splitlines()[-1]


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


def _await_controller(port, controller, timeout=30.0):
    """The key state this witness holds for a controller, once it has finished accepting it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return _get(port, f"/v1/witness/controller/{controller}")
        except urllib.error.HTTPError:
            time.sleep(0.5)
    raise AssertionError(f"the witness never reported key state for {controller}")


def _witness_for(container, port, bran, alias):
    """Have a controller incept with this witness designated, and return what the witness holds.

    Returns (witness_aid, controller_aid, key_state). The assertions are here rather than at each
    call site because every caller wants the same three, and a caller that forgot one would be
    proving less than it reads as proving.
    """
    witness_aid = _get(port, "/v1/witness/identity")["aid"]
    accepted = _submit_inception(container, witness_aid, bran, alias)
    assert accepted["status"] == 204
    controller = accepted["controller"]
    state = _await_controller(port, controller)
    assert state["witnesses"] == [witness_aid], "the witness should be designated"
    return witness_aid, controller, state


def _volume_for(image):
    name = f"witness-upgrade-{uuid.uuid4().hex[:8]}"
    _docker("volume", "create", name)
    _docker(
        "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "--entrypoint", "kli", image,
        "init", "--name", "witness", "--nopasscode",
    )
    containers = []
    try:
        yield name, containers
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        # Swept by PREFIX rather than by the single name created here. A test that makes a
        # second volume -- the backup one makes <name>-backup -- otherwise leaves it behind on
        # every local run, and CI hides that by being ephemeral. `--filter name=` is a substring
        # match, which is what makes one call cover both.
        listed = _docker("volume", "ls", "-q", "--filter", f"name={name}", check=False)
        for leftover in listed.stdout.split():
            _docker("volume", "rm", "-f", leftover, check=False)


@pytest.fixture
def volume():
    yield from _volume_for(IMAGE)


@pytest.fixture
def baseline_volume():
    """A volume created and initialised by the image being upgraded FROM (@ktljhcyt).

    The keystore matters as much as the database here: `kli init` runs from the baseline, so the
    stores this image inherits are in every respect the ones the deployed version wrote.
    """
    yield from _volume_for(BASELINE)


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
    witness_aid, before_aid, before_state = _witness_for(
        first, port_a, "0987654321kjihgfedcba", "before"
    )

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
    _witness_for(second, port_b, "mnopqrstuvw1122334455", "after")


def _image_id(image):
    """The local image ID a reference resolves to, which is what makes two references comparable.

    Two references can name one image -- a tag and the digest it points at, or a stale variable
    pointing at the build under test -- and the digests in their names would still differ.
    """
    return _docker("image", "inspect", image, "--format", "{{.Id}}").stdout.strip()


def _two_images_or_fail():
    """The handover's own premise: the baseline is a DIFFERENT image from the candidate.

    FAILS rather than skips, unlike the pin check below, and the difference is the point. An unset
    baseline is a claim nobody made, which is a skip. A baseline that resolves to the candidate is
    a claim made falsely: the test would pass, having replaced a container with itself, and report
    cross-version compatibility it never exercised. That is the same hole the absent-rather-than-
    defaulted rule closes, reached by a stale variable instead of a missing one -- and the likely
    route is real, since DEPLOYED_WITNESS_IMAGE points at this build the moment infra deploys it.
    Raised by Copilot on #17.
    """
    if _image_id(IMAGE) == _image_id(BASELINE):
        pytest.fail(
            f"WITNESS_BASELINE_IMAGE ({BASELINE}) resolves to the same image as WITNESS_IMAGE "
            f"({IMAGE}), so this would replace a container with itself and prove nothing about "
            "upgrading. Point it at the digest being upgraded FROM, or unset it and let the "
            "handover tests skip."
        )


def _same_pin_or_skip():
    """Both images must carry the same keripy, or the handover is a different operation.

    When the pins differ the upgrade crosses a migration: keripy refuses to open a database
    written by a newer library, `kli migrate run` is required in one direction and the other
    direction does not work at all (@a24p3kbw, docs/deploying.md). Failing here would read as a
    broken upgrade when what actually happened is that the test was pointed at a pin bump, so this
    skips and names both versions. Rehearsing the migration itself is separate work.
    """
    candidate, baseline = _keripy_version(IMAGE), _keripy_version(BASELINE)
    if candidate != baseline:
        pytest.skip(
            f"the baseline carries keripy {baseline} and this image carries {candidate}, so this "
            "upgrade crosses a migration rather than replacing a container -- a one-way door with "
            "its own procedure in docs/deploying.md, which this oracle does not exercise"
        )
    return candidate


@needs_baseline
def test_this_image_takes_over_a_volume_the_deployed_one_wrote(baseline_volume):
    """The claim every upgrade actually makes, which the same-image test could not make.

    Its sibling above replaces a container over a volume, which proves that replacing a CONTAINER
    is safe. Both containers are the same image there, so version compatibility -- the one
    property an operator is buying when a digest changes -- was the part not under test.

    Here the baseline creates the volume, incepts the keystore, and gets a controller witnessed;
    then this image takes the volume over and has to serve the same AID, return the same key
    state, and accept a FURTHER event. The last clause is again the one with teeth: holding the
    history proves storage, receipting a new event proves the signing keys came across too.
    """
    _two_images_or_fail()
    _same_pin_or_skip()
    name, containers = baseline_volume
    old, new = f"{name}-old", f"{name}-new"
    port_old, port_new = _free_port(), _free_port()

    containers.append(old)
    _start(name, old, port_old, image=BASELINE)
    witness_aid, controller, before = _witness_for(old, port_old, "0987654321kjihgfedcba", "before")

    _docker("rm", "-f", old)
    containers.append(new)
    _start(name, new, port_new)

    assert _get(port_new, "/v1/witness/identity")["aid"] == witness_aid, (
        "the witness lost its AID when this image took over from the deployed one; every "
        "controller that designated it would have to rotate"
    )
    assert _get(port_new, f"/v1/witness/controller/{controller}") == before, (
        "the witnessed key state did not survive the version change"
    )

    _, fresh, _ = _witness_for(new, port_new, "mnopqrstuvw1122334455", "after")
    assert fresh != controller


@needs_baseline
def test_the_deployed_image_takes_the_volume_back(baseline_volume):
    """The rollback direction, which within one keripy pin is just redeploying the old digest.

    docs/deploying.md promises exactly that -- "within a pin, rollback is just redeploying the
    previous digest and is free" -- and a rollback nobody has run is a hope, which is @7b34ohbo's
    argument applied one level up. It is also the direction that fails first if this image writes
    anything the previous one cannot read, and the harm from discovering that during an incident
    is that the escape hatch is the thing that is broken.
    """
    _two_images_or_fail()
    _same_pin_or_skip()
    name, containers = baseline_volume
    new, back = f"{name}-new", f"{name}-back"
    port_new, port_back = _free_port(), _free_port()

    containers.append(new)
    _start(name, new, port_new)
    witness_aid, controller, written = _witness_for(new, port_new, "0987654321kjihgfedcba", "fwd")

    _docker("rm", "-f", new)
    containers.append(back)
    _start(name, back, port_back, image=BASELINE)

    assert _get(port_back, "/v1/witness/identity")["aid"] == witness_aid
    assert _get(port_back, f"/v1/witness/controller/{controller}") == written, (
        "the deployed image could not read what this one wrote, so redeploying the previous "
        "digest is not a rollback path and the runbook is wrong to say it is"
    )

    _, fresh, _ = _witness_for(back, port_back, "mnopqrstuvw1122334455", "rolled")
    assert fresh != controller


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


def test_a_witness_restored_from_backup_still_witnesses(volume):
    """The rollback path, exercised rather than assumed (@7b34ohbo).

    @a24p3kbw made an upgrade across a keripy migration a one-way door, so restoring a backup is
    how a deployment goes backwards. This destroys the volume outright — not a corruption, a
    deletion — and rebuilds from the backup alone.

    The last assertion is the one with teeth: the restored witness must accept and RECEIPT a new
    event, which it can only do with its signing keys. A backup of the event database alone would
    pass every earlier check in this test and fail that one, which is exactly the failure mode
    worth a test rather than a comment.
    """
    name, containers = volume
    backup_volume = f"{name}-backup"
    live, restored = f"{name}-live", f"{name}-restored"
    port_a, port_b = _free_port(), _free_port()

    _docker("volume", "create", backup_volume)
    try:
        containers.append(live)
        _docker(
            "run", "-d", "--name", live,
            "-v", f"{name}:{_KERI_HOME}", "-v", f"{backup_volume}:/backup",
            "-p", f"127.0.0.1:{port_a}:5633", IMAGE,
        )
        _await_ready(live, port_a)
        witness_aid, controller, witnessed = _witness_for(
            live, port_a, "0987654321kjihgfedcba", "pre"
        )

        # Back up WHILE THE WITNESS RUNS — the property env.copy buys over stopping and tarring.
        result = _docker(
            "exec", live, "witness", "backup", "--name", "witness", "--to", "/backup/snapshot",
        )
        manifest = json.loads(result.stdout[result.stdout.index("{"):])
        assert manifest["stores"]["keystore"]["present"] is True
        assert manifest["witness_aid"] == witness_aid

        # Destroy the witness entirely: container and volume both gone.
        _docker("rm", "-f", live)
        containers.remove(live)
        _docker("volume", "rm", "-f", name)

        # Restore is a directory copy, which is why the backup mirrors the keri layout.
        _docker("volume", "create", name)
        _docker(
            "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "-v", f"{backup_volume}:/backup",
            "--entrypoint", "sh", IMAGE, "-c", f"cp -a /backup/snapshot/. {_KERI_HOME}/",
        )

        containers.append(restored)
        _start(name, restored, port_b)

        assert _get(port_b, "/v1/witness/identity")["aid"] == witness_aid
        assert _get(port_b, f"/v1/witness/controller/{controller}") == witnessed

        # It holds the history AND can still sign; a backup missing the keystore passes everything
        # above and fails here, which is the whole reason this assertion is last rather than first.
        _witness_for(restored, port_b, "mnopqrstuvw1122334455", "post")
    finally:
        _docker("volume", "rm", "-f", backup_volume, check=False)


def test_a_restore_that_lost_the_keystore_refuses_to_start(volume):
    """~5dnx, the disaster this oracle exists to make impossible.

    Backups are the rollback path (@7b34ohbo), so a restore that copies the event database and
    not the keystore is a real mistake with a real occasion. What used to happen next was the
    worst available outcome: keripy created a fresh keystore, and the witness came up serving the
    AID out of the database — the one every controller designated and every validator trusts —
    while signing with keys that AID does not name. Health said `ok`, inceptions were accepted,
    `witness backup` succeeded. Nothing observable was wrong.

    So the refusal has to be proven against the real image, on a real volume, by actually taking
    the keystore away. Asserting it in a unit test proves the branch; asserting it here proves the
    container.
    """
    name, containers = volume
    first, second = f"{name}-k1", f"{name}-k2"
    containers.extend([first, second])

    _start(name, first, _free_port())
    _docker("rm", "-f", first)

    # Exactly what a db-only restore leaves behind: history, no keys.
    _docker(
        "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "--entrypoint", "sh", IMAGE,
        "-c", f"rm -rf {_KERI_HOME}/ks",
    )

    _docker(
        "run", "-d", "--name", second,
        "-v", f"{name}:{_KERI_HOME}",
        "-p", f"127.0.0.1:{_free_port()}:5633",
        IMAGE,
    )

    deadline = time.time() + 60
    while time.time() < deadline:
        running = _docker("inspect", "-f", "{{.State.Running}}", second, check=False)
        if running.stdout.strip() != "true":
            break
        time.sleep(1)
    else:
        raise AssertionError("the container kept running over a volume with no keystore")

    logs = _docker("logs", second, check=False)
    output = logs.stdout + logs.stderr
    assert "e.state.conflict.keystore.f" in output or "KeystoreLost" in output, (
        f"the container stopped without saying why:\n{output}"
    )
    # `test -e`, not a substring of `ls`: the directory prints as `ks`, so searching that output
    # for the absolute path could never match and the assertion could never fail.
    still_gone = _docker(
        "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "--entrypoint", "sh", IMAGE,
        "-c", f"test ! -e {_KERI_HOME}/ks", check=False,
    )
    assert still_gone.returncode == 0, (
        "the refusal created the keystore it refused over, which is the branch it exists to "
        "prevent reaching"
    )
