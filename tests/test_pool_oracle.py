"""Pool oracle: `witness pool` against the real image (@n2bgpdds).

``tests/test_pool.py`` pins the policy with Docker injected. This runs it, and exists for the two
claims that only a real container can settle.

The first is the seeded config. A witness advertises where to reach it through keripy's config
file, read exactly once when the hab is created, and the pool seeds it into the volume at keripy's
own default path before the first start. Nothing short of resolving a real OOBI proves that landed
— a witness with no config serves an OOBI too, just one that names no location, and the difference
is invisible until something tries to use it.

The second is that ``down`` is complete. Cleanup keys on labels rather than on bookkeeping
precisely so an interrupted ``up`` still tears down, and the way to show it is to ask Docker
afterwards rather than to trust what the pool said it did.

Skipped unless WITNESS_IMAGE names an image, like the other oracles, and deliberately outside the
coverage gate.
"""

import contextlib
import json
import os
import socket
import subprocess
import urllib.request
import uuid

import pytest

from witness.config import PoolConfig
from witness.pool import LABEL, Pool

IMAGE = os.environ.get("WITNESS_IMAGE")

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set WITNESS_IMAGE to the image under test to run the pool oracle"
)


def _docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=300
    )


def _free(port):
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_base(count):
    """A base port whose whole pool is free, since the pool's ports are consecutive by design."""
    for base in range(5700, 5900, 10):
        if all(_free(base + 10 * index + offset) for index in range(count) for offset in (0, 2)):
            return base
    raise AssertionError("no free port block for the pool")


def _run(verb, name, **kwargs):
    """One pool verb with the real Docker and HTTP seams, capturing what it printed."""
    written = []
    config = PoolConfig(verb=verb, name=name, image=IMAGE, **kwargs)
    assert Pool(config, out=written.append).run() == 0
    return "".join(written)


@pytest.fixture
def pool():
    """A two-witness pool, torn down however the test ends."""
    name = f"oracle-{uuid.uuid4().hex[:8]}"
    try:
        yield name, _free_base(2)
    finally:
        # Deliberately NOT `witness pool down`: a test must not tidy up with the code it is
        # testing, or a down that quietly does nothing would leave every run green and the host
        # full of containers.
        containers = _names(name)
        if containers:
            _docker("rm", "-f", *containers, check=False)
        volumes = _volumes(name)
        if volumes:
            _docker("volume", "rm", "-f", *volumes, check=False)


def _names(name):
    listed = _docker("ps", "-a", "--filter", f"label={LABEL}={name}", "--format", "{{.Names}}")
    return [line for line in listed.stdout.splitlines() if line.strip()]


def _volumes(name):
    listed = _docker("volume", "ls", "--filter", f"label={LABEL}={name}", "--quiet")
    return [line for line in listed.stdout.splitlines() if line.strip()]


def test_a_pool_comes_up_resolves_and_goes_away_completely(pool):
    name, base = pool

    report = _run("up", name, count=2, base_port=base, timeout=180)
    assert "2 witnesses" in report
    assert len(_names(name)) == 2 and len(_volumes(name)) == 2

    manifest = json.loads(_run("manifest", name))
    assert [witness["alias"] for witness in manifest["witnesses"]] == ["w1", "w2"]
    assert manifest["toad"] == 2, "keripy's own ample(2)"

    for witness in manifest["witnesses"]:
        assert witness["aid"].startswith("B"), "a witness AID is non-transferable"

        # The claim only a real witness can settle: the seeded config became a location the
        # witness actually serves, so an OOBI resolved from this URL learns where to come back to.
        with urllib.request.urlopen(witness["oobi"], timeout=20) as answer:  # noqa: S310
            introduction = answer.read().decode()
        assert answer.status == 200
        assert witness["aid"] in introduction
        assert witness["http"].rstrip("/") in introduction, (
            "the witness introduces itself without naming where it lives, so the config the pool "
            "seeded was not read"
        )

    _run("down", name)
    assert _names(name) == [] and _volumes(name) == [], "down must leave nothing behind"


def test_a_broken_witness_stops_answering_and_a_healed_one_answers_again(pool):
    name, base = pool
    _run("up", name, count=2, base_port=base, timeout=180)

    assert "ok" in _run("status", name)

    _run("break", name, witness="w2")
    broken = _run("status", name)
    assert "w2  exited" in broken
    assert "w1  running" in broken, "breaking one witness must not disturb the other"

    _run("heal", name, witness="w2")
    assert "w2  running" in _run("status", name)
