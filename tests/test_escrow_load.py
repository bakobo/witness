"""Load oracle: what a flood of unanswerable queries costs the witness (~4ekl).

`Kevery.processQueryNotFound` re-walks the entire query-not-found escrow on every hio loop pass,
so a `/query` for an AID this witness does not hold buys the attacker per-pass work for up to
`TimeoutQNF` (300 s). That much was readable in keripy. What was NOT readable, and what this
measures, is the *slope*: how much a given escrow depth actually costs, and at what depth a real
controller starts waiting.

The measured curve is in `docs/escrow-load.md`. The assertions here are deliberately about SHAPE
rather than about absolute times, which are machine-dependent: cost grows with depth, and the
escrow drains on its own. Pinning a millisecond figure would make this a test of the runner it
happens to execute on.

Skipped unless WITNESS_IMAGE names an image, and outside the coverage gate, like the other
oracles. Slow — it deliberately builds thousands of escrow entries — so it is also gated behind
WITNESS_LOAD to keep it out of the ordinary image run.
"""

import json
import os
import subprocess
import time
import urllib.request
import uuid

import pytest

IMAGE = os.environ.get("WITNESS_IMAGE")

pytestmark = pytest.mark.skipif(
    not (IMAGE and os.environ.get("WITNESS_LOAD")),
    reason="set WITNESS_IMAGE and WITNESS_LOAD to run the escrow load oracle (slow)",
)

_KERI_HOME = "/usr/local/var/keri"

#: Runs inside the container so the measurement is of the witness rather than of the docker
#: bridge. Builds one querier and sends `count` queries for an AID the witness does not hold;
#: every query carries a distinct SAID, so each parks its own escrow entry.
_FLOOD = """
import json, sys, urllib.request
from keri.app import habbing
from keri.app.httping import CESR_CONTENT_TYPE
from keri.core import eventing
from keri.kering import Kinds

witness, count = sys.argv[1], int(sys.argv[2])
unheld = "EAAAAoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
hby = habbing.Habery(name="flood", base="", temp=True, bran="0987654321kjihgfedcba")
hab = hby.makeHab(name="flood", transferable=True)
for _ in range(count):
    serder = eventing.query(pre=hab.pre, route="logs",
                            query={"i": unheld, "src": witness}, kind=Kinds.json)
    message = bytes(hab.endorse(serder, last=True, framed=False))
    request = urllib.request.Request(
        "http://127.0.0.1:5631/", data=serder.raw, method="POST",
        headers={"Content-Type": CESR_CONTENT_TYPE,
                 "CESR-ATTACHMENT": message[len(serder.raw):].decode()})
    urllib.request.urlopen(request, timeout=20)
hby.close()
print(json.dumps({"sent": count}))
"""

_SAMPLE = """
import json, urllib.request
def get(path):
    return json.load(urllib.request.urlopen("http://127.0.0.1:5633" + path, timeout=30))
loop = get("/v1/witness/loop")
print(json.dumps({
    "qnfs": get("/v1/witness/escrow")["depths"]["query_not_found"],
    "lag": loop["loop_lag"],
    "health": get("/v1/witness/health")["status"],
    "slowest": max(d["last_seconds"] for d in loop["doers"]),
}))
"""


def _docker(*args, **kwargs):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=900, **kwargs
    )


def _exec_json(container, script, *args):
    result = _docker("exec", container, "python", "-c", script, *args)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def witness():
    name = f"witness-load-{uuid.uuid4().hex[:8]}"
    _docker("volume", "create", name)
    _docker(
        "run", "--rm", "-v", f"{name}:{_KERI_HOME}", "--entrypoint", "kli", IMAGE,
        "init", "--name", "witness", "--nopasscode",
    )
    _docker("run", "-d", "--name", name, "-v", f"{name}:{_KERI_HOME}", IMAGE)
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if _exec_json(name, _SAMPLE)["qnfs"] == 0:
                    break
            except Exception:  # noqa: BLE001 - the control plane is still coming up
                time.sleep(1)
        aid = _exec_json(
            name,
            "import json,urllib.request;"
            "print(json.dumps(json.load(urllib.request.urlopen("
            "'http://127.0.0.1:5633/v1/witness/identity'))))",
        )["aid"]
        yield name, aid
    finally:
        _docker("rm", "-f", name)
        _docker("volume", "rm", "-f", name)


def test_escrow_cost_grows_with_depth_and_the_witness_stays_up(witness):
    """The shape of the harm, and its limit.

    Cost per pass rises with escrow depth — that is the amplification. But the witness keeps
    answering throughout, which is why this is degradation rather than denial, and why the
    recorded disposition is hardening rather than an incident.
    """
    container, aid = witness

    baseline = _exec_json(container, _SAMPLE)
    assert baseline["qnfs"] == 0

    _exec_json(container, _FLOOD, aid, "300")
    time.sleep(2)
    loaded = _exec_json(container, _SAMPLE)

    assert loaded["qnfs"] >= 200, "the flood should have parked escrow entries"
    assert loaded["slowest"] > baseline["slowest"], (
        "escrow depth should cost the loop measurably more per pass"
    )
    assert loaded["health"] in {"ok", "degraded"}, "the witness must still be answering"


def test_health_does_not_catch_degradation_only_a_wedge(witness):
    """Deliberate, and worth pinning so nobody 'fixes' it by accident.

    Health answers "is this witness working" and a degraded witness IS working, slowly. Loop lag
    is the graded signal and belongs on a threshold an operator sets from `witness.loop.lag`;
    baking one into a binary endpoint would make it flap. Measured: at a depth costing 0.33 s of
    lag — ten times the tock — health still reports ok, which is correct and needs saying.
    """
    container, aid = witness

    _exec_json(container, _FLOOD, aid, "300")
    time.sleep(2)
    loaded = _exec_json(container, _SAMPLE)

    assert loaded["lag"] > 0, "lag is the signal that moves"
    assert loaded["health"] == "ok", (
        "health reports wedges, not slowness — if this ever fails, decide deliberately whether "
        "health should grade degradation, and record it in this.i rather than here"
    )


def test_the_escrow_drains_on_its_own(witness):
    """TimeoutQNF bounds the exposure: entries age out at 300 s, so a flood that stops is a
    witness that recovers without anyone doing anything. That is what makes edge rate limiting
    a real mitigation here rather than a partial one."""
    container, aid = witness

    _exec_json(container, _FLOOD, aid, "200")
    time.sleep(2)
    peak = _exec_json(container, _SAMPLE)["qnfs"]
    assert peak > 0

    # TimeoutQNF is 300 s; wait past it rather than guessing at a shorter window.
    time.sleep(330)
    drained = _exec_json(container, _SAMPLE)["qnfs"]

    assert drained < peak, f"escrow did not drain: {peak} -> {drained}"
