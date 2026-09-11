"""``witness pool`` — throwaway pools of this image, for experiments (@n2bgpdds).

A pool is N containers of the witness image, each with its own volume, keystore, PID namespace and
published ports, plus one command that removes all of it again. It exists because the questions
worth asking of a witness — what a controller's toad buys, what happens when one witness goes
away, what the control plane says while it does — need more than one witness to ask, and because
the thing worth asking them of is the *image* rather than a process in a developer's virtualenv.

Three properties are worth knowing before changing anything here.

**Cleanup keys on labels, not on bookkeeping.** Every container and volume carries
``bakobo.pool=<name>``, so ``down`` removes what exists rather than what some state file claims was
created. An ``up`` interrupted between the volume and the container still tears down in full.

**There is no state file at all.** The manifest is derived live, from the labels and from each
control plane's own ``/v1/witness/identity``, so what the pool reports cannot drift from what is
running — and ``down`` leaves nothing behind on disk to clean up separately.

**The witness's advertised URL is seeded into the volume, not passed on the command line.** keripy
reads a hab's config exactly once, when ``makeHab`` creates it, so the file has to be in place
before the container's first start; and it goes to keripy's OWN default location inside the volume
(``/usr/local/var/keri/cf/<name>.json``) rather than arriving via ``--config-dir`` in an overridden
command, because overriding the command would mean restating the image's CMD here, where it would
drift silently the first time the image's own command changed.

The pool never incepts anything (@jorbhpfq). It serves witnesses and describes them; whatever holds
keys — heti's lockbox, ``kli incept`` — does the designating, which is what ``manifest`` feeds.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from keri.core.eventing import ample
from keri.core.signing import Salter
from keri.help.helping import nowIso8601

from .errors import (
    DockerUnavailable,
    InvalidArguments,
    PoolExists,
    PoolNotReady,
    PoolUnknown,
    WitnessUnknown,
)

#: The label every container and volume carries, and the whole of the pool's bookkeeping.
LABEL = "bakobo.pool"
WITNESS_LABEL = f"{LABEL}.witness"
HTTP_LABEL = f"{LABEL}.http"
CONTROL_LABEL = f"{LABEL}.control"

#: Where the image mounts the keri home, and the keystore/alias its CMD uses. Both are the image's
#: own choices (see the Dockerfile); a pool that disagreed with either would seed a config keripy
#: never reads.
KERI_HOME = "/usr/local/var/keri"
_ALIAS = "witness"

#: The image's in-container ports (Dockerfile EXPOSE): witness HTTP and control plane. TCP (5632)
#: is deliberately not published — infra closes it too, and nothing in an experiment needs it.
_WITNESS_PORT = 5631
_CONTROL_PORT = 5633

#: Host ports: witness i takes base + 10(i-1), with its control plane two above. Ten leaves room
#: for the TCP port to be published later without renumbering a pool.
_PORT_STRIDE = 10
_CONTROL_OFFSET = 2
_MAX_PORT = 65535

#: Bounded before it is trusted. Twenty-five witnesses is far past any experiment and well short of
#: what would exhaust a laptop by accident.
_MAX_COUNT = 25
_POLL_INTERVAL = 0.5
_REQUEST_TIMEOUT = 5.0

#: Seeds the witness's own config into the volume using keripy's Configer, so the path, the
#: permissions and the encoding are keripy's rather than a guess made here. Run inside a one-shot
#: container as the image's own uid, before the witness ever starts.
_SEED_CONFIG = (
    "import json,sys\n"
    "from keri.app.configing import Configer\n"
    "cf=Configer(name=sys.argv[1],base='',temp=False,reopen=True,clear=False)\n"
    "cf.put(json.loads(sys.argv[2]))\n"
)

_PS_FORMAT = (
    "{{.Names}}\t"
    f'{{{{.Label "{WITNESS_LABEL}"}}}}\t'
    f'{{{{.Label "{HTTP_LABEL}"}}}}\t'
    f'{{{{.Label "{CONTROL_LABEL}"}}}}\t'
    "{{.State}}\t{{.Image}}"
)
_LS_FORMAT = "{{.Names}}\t" f'{{{{.Label "{LABEL}"}}}}\t' "{{.Image}}"


def run_docker(args, *, check=True, timeout=300):
    """Run one docker command, turning every way it can fail into one typed error.

    ``check=False`` is for the commands whose failure is not news — removing something that is
    already gone — and even those come back as a captured result rather than an exception.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, arguments built here
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as missing:
        raise DockerUnavailable(
            "I could not find the docker command, so I cannot build a pool. `witness pool` runs on "
            "a host with Docker, not inside the witness image."
        ) from missing
    except subprocess.TimeoutExpired as slow:
        raise DockerUnavailable(
            f"Docker did not answer `{' '.join(args)}` within {timeout} seconds."
        ) from slow
    if check and result.returncode != 0:
        raise DockerUnavailable(
            f"Docker refused `{' '.join(args)}`: {result.stderr.strip() or 'no reason given'}."
        )
    return result.stdout


def fetch_json(url, *, timeout=_REQUEST_TIMEOUT):
    """GET a control-plane document, or ``None`` if that witness did not answer.

    Unreachable is an ordinary state for a pool — a stopped witness is the point of ``break`` — so
    it is a value here rather than an exception, and the caller decides whether it matters.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class Witness:
    """One witness of a pool, as the labels on its container describe it."""

    alias: str
    container: str
    http_port: int
    control_port: int
    state: str
    image: str

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/"

    @property
    def control_url(self) -> str:
        return f"http://127.0.0.1:{self.control_port}/"

    def oobi(self, aid: str) -> str:
        """Where this witness introduces itself — the URL heti's standing witness set holds."""
        return f"http://127.0.0.1:{self.http_port}/oobi/{aid}/controller"


class Pool:
    """Every ``witness pool`` verb, over injected Docker and HTTP."""

    def __init__(
        self,
        config,
        *,
        docker=run_docker,
        fetch=fetch_json,
        sleep=time.sleep,
        clock=time.monotonic,
        now=nowIso8601,
        out=None,
    ):
        self._config = config
        self._docker = docker
        self._fetch = fetch
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._out = out if out is not None else _stdout

    def run(self) -> int:
        """Dispatch the verb and return the process exit code."""
        verbs = {
            "up": self._up,
            "down": self._down,
            "status": self._status,
            "break": self._break,
            "heal": self._heal,
            "manifest": self._manifest,
            "ls": self._ls,
        }
        verbs[self._config.verb]()
        return 0

    # -- verbs ---------------------------------------------------------------------------------

    def _up(self):
        count, image = self._planned()
        if self._containers():
            raise PoolExists(
                f"A pool named {self._config.name} is already running. Take it down with "
                f"`witness pool down --name {self._config.name}`, or choose another --name."
            )
        # Built from what was just created rather than read back from `docker ps`: this code knows
        # exactly what it made, and a round trip could only disagree with it.
        witnesses = [self._create(index, image) for index in range(1, count + 1)]
        self._say(f"pool {self._config.name} — {count} witnesses on {image}")
        for witness in witnesses:
            aid = self._aid(witness, wait=True)
            self._say(
                f"  {witness.alias}  {aid}  {witness.http_url}  control {witness.control_url}"
            )
        self._say(
            f"`witness pool manifest --name {self._config.name} --format heti` describes it; "
            f"`witness pool down --name {self._config.name}` removes it."
        )

    def _down(self):
        containers = self._containers(every=self._config.all_pools)
        volumes = self._volumes(every=self._config.all_pools)
        if not containers and not volumes:
            raise PoolUnknown(self._nothing_there())
        if containers:
            self._docker(["rm", "-f", *[witness.container for witness in containers]])
        if volumes:
            self._docker(["volume", "rm", "-f", *volumes])
        scope = "every pool" if self._config.all_pools else f"pool {self._config.name}"
        self._say(
            f"removed {scope}: {len(containers)} containers and {len(volumes)} volumes. "
            "Nothing else was written, so there is nothing else to clean up."
        )

    def _status(self):
        for witness in self._sorted(self._require_containers()):
            self._say(f"  {witness.alias}  {witness.state}  {self._condition(witness)}")

    def _break(self):
        witness = self._named()
        command = "pause" if self._config.mode == "pause" else "stop"
        self._docker([command, witness.container])
        wording = "frozen mid-flight" if command == "pause" else "stopped"
        self._say(
            f"  {witness.alias} is {wording}. `witness pool heal --name {self._config.name} "
            f"--witness {witness.alias}` puts it back."
        )

    def _heal(self):
        witness = self._named()
        if witness.state == "paused":
            self._docker(["unpause", witness.container])
        elif witness.state == "running":
            self._say(f"  {witness.alias} is already running; nothing to do.")
            return
        else:
            self._docker(["start", witness.container])
        self._say(f"  {witness.alias} is back in service.")

    def _manifest(self):
        witnesses = self._sorted(self._require_containers())
        aids = [self._aid(witness) for witness in witnesses]
        toad = self._config.toad if self._config.toad is not None else ample(len(witnesses))
        oobis = [witness.oobi(aid) for witness, aid in zip(witnesses, aids, strict=True)]
        if self._config.fmt == "heti":
            self._say("[witnesses]")
            self._say("oobis = [")
            for oobi in oobis:
                self._say(f'  "{oobi}",')
            self._say("]")
            self._say(f"toad = {toad}")
            return
        if self._config.fmt == "kli":
            # Every key `kli incept --file` requires; omitting one makes it refuse the document.
            self._emit({
                "transferable": True,
                "wits": aids,
                "toad": toad,
                "icount": 1,
                "isith": "1",
                "ncount": 1,
                "nsith": "1",
            })
            return
        self._emit({
            "pool": self._config.name,
            "image": witnesses[0].image,
            "toad": toad,
            "witnesses": [
                {
                    "alias": witness.alias,
                    "aid": aid,
                    "http": witness.http_url,
                    "oobi": oobi,
                    "control": witness.control_url,
                }
                for witness, aid, oobi in zip(witnesses, aids, oobis, strict=True)
            ],
        })

    def _ls(self):
        listed = self._docker([
            "ps", "-a", "--filter", f"label={LABEL}", "--format", _LS_FORMAT
        ])
        pools = {}
        for line in listed.splitlines():
            if not line.strip():
                continue
            _name, pool, image = line.split("\t")[:3]
            count, _image = pools.get(pool, (0, image))
            pools[pool] = (count + 1, image)
        if not pools:
            self._say("There are no pools on this host.")
            return
        for pool, (count, image) in sorted(pools.items()):
            self._say(f"  {pool}  {count} witnesses  {image}")

    # -- the pieces the verbs are made of --------------------------------------------------------

    def _planned(self):
        """Validate what ``up`` was asked for before it creates anything."""
        count = self._config.count
        if not 1 <= count <= _MAX_COUNT:
            raise InvalidArguments(
                f"A pool holds between 1 and {_MAX_COUNT} witnesses, but --count was {count}."
            )
        top = self._config.base_port + _PORT_STRIDE * (count - 1) + _CONTROL_OFFSET
        if top > _MAX_PORT:
            raise InvalidArguments(
                f"A pool of {count} witnesses from --base-port {self._config.base_port} would "
                f"need port {top}, above {_MAX_PORT}."
            )
        if not self._config.image:
            raise InvalidArguments(
                "I was not told which image to run. Pass --image, or set WITNESS_IMAGE. There is "
                "deliberately no floating tag to fall back on."
            )
        return count, self._config.image

    def _create(self, index, image) -> Witness:
        """Volume, keystore, config, container — in that order, which is not interchangeable."""
        name = f"witness-pool-{self._config.name}-w{index}"
        alias = f"w{index}"
        http_port = self._config.base_port + _PORT_STRIDE * (index - 1)
        control_port = http_port + _CONTROL_OFFSET
        labels = [
            "--label", f"{LABEL}={self._config.name}",
            "--label", f"{WITNESS_LABEL}={alias}",
            "--label", f"{HTTP_LABEL}={http_port}",
            "--label", f"{CONTROL_LABEL}={control_port}",
        ]
        self._docker(["volume", "create", *labels, name])
        init = ["init", "--name", _ALIAS, "--nopasscode"]
        if self._config.seed is not None:
            init += ["--salt", _salt(self._config.seed, alias)]
        self._docker([
            "run", "--rm", "-v", f"{name}:{KERI_HOME}", "--entrypoint", "kli", image, *init
        ])
        config = {_ALIAS: {"dt": self._now(), "curls": [f"http://127.0.0.1:{http_port}/"]}}
        self._docker([
            "run", "--rm", "-v", f"{name}:{KERI_HOME}", "--entrypoint", "python", image,
            "-c", _SEED_CONFIG, _ALIAS, json.dumps(config, separators=(",", ":")),
        ])
        self._docker([
            "run", "-d", "--name", name, *labels,
            "-v", f"{name}:{KERI_HOME}",
            "-p", f"127.0.0.1:{http_port}:{_WITNESS_PORT}",
            "-p", f"127.0.0.1:{control_port}:{_CONTROL_PORT}",
            image,
        ])
        return Witness(
            alias=alias,
            container=name,
            http_port=http_port,
            control_port=control_port,
            state="running",
            image=image,
        )

    def _containers(self, every=False):
        """Every container carrying the pool label, in docker's own order."""
        selector = f"label={LABEL}" if every else f"label={LABEL}={self._config.name}"
        listed = self._docker(["ps", "-a", "--filter", selector, "--format", _PS_FORMAT])
        witnesses = []
        for line in listed.splitlines():
            if not line.strip():
                continue
            container, alias, http_port, control_port, state, image = line.split("\t")[:6]
            witnesses.append(Witness(
                alias=alias,
                container=container,
                http_port=int(http_port),
                control_port=int(control_port),
                state=state,
                image=image,
            ))
        return witnesses

    def _require_containers(self):
        containers = self._containers()
        if not containers:
            raise PoolUnknown(self._nothing_there())
        return containers

    def _volumes(self, every=False):
        selector = f"label={LABEL}" if every else f"label={LABEL}={self._config.name}"
        listed = self._docker(["volume", "ls", "--filter", selector, "--quiet"])
        return [line for line in listed.splitlines() if line.strip()]

    def _named(self):
        """The witness ``--witness`` names, or a refusal that says which names exist."""
        if not self._config.witness:
            raise InvalidArguments(
                "I was not told which witness to act on. Pass --witness, e.g. --witness w2."
            )
        witnesses = self._sorted(self._require_containers())
        for witness in witnesses:
            if witness.alias == self._config.witness:
                return witness
        names = ", ".join(witness.alias for witness in witnesses)
        raise WitnessUnknown(
            f"Pool {self._config.name} has no witness named {self._config.witness}. It has: {names}."
        )

    def _aid(self, witness, wait=False):
        """This witness's own AID, waiting for its loop to turn first when asked to.

        ``wait`` is what ``up`` needs and what ``manifest`` does not: a pool that has just been
        created is still opening its keystore, while a pool being described has been up for a
        while and a silent witness means something is wrong.
        """
        if wait:
            self._await_ready(witness)
        identity = self._fetch(f"{witness.control_url}v1/witness/identity")
        if not identity or not identity.get("aid"):
            raise PoolNotReady(
                f"Witness {witness.alias} of pool {self._config.name} did not tell me its AID. "
                f"`docker logs {witness.container}` will say why."
            )
        return identity["aid"]

    def _await_ready(self, witness):
        """Wait for a turning loop, which is what ``ready`` means for a witness.

        An answering control plane is not enough: health reports 200 as soon as the database
        opens, which can be before the runner has published a single tick.
        """
        deadline = self._clock() + self._config.timeout
        while self._clock() < deadline:
            health = self._fetch(f"{witness.control_url}v1/witness/health")
            if health and health.get("status") == "ok" and health.get("ticks"):
                return
            self._sleep(_POLL_INTERVAL)
        raise PoolNotReady(
            f"Witness {witness.alias} of pool {self._config.name} did not report a turning loop "
            f"within {self._config.timeout:g} seconds. Its container is still running, so "
            f"`docker logs {witness.container}` will say why."
        )

    def _condition(self, witness):
        """What this witness says about itself right now, in one line's worth of words."""
        if witness.state != "running":
            return "not running"
        health = self._fetch(f"{witness.control_url}v1/witness/health")
        if not health:
            return "unreachable"
        controllers = self._fetch(f"{witness.control_url}v1/witness/controller") or {}
        held = len(controllers.get("controllers", []))
        status = health.get("status", "unknown")
        return f"{status}  ticks {health.get('ticks', 0)}  controllers {held}"

    def _nothing_there(self):
        return (
            f"I found no pool named {self._config.name} on this host. `witness pool ls` lists the "
            "pools there are."
        )

    @staticmethod
    def _sorted(witnesses):
        """Pool order, which is not docker's — ``docker ps`` answers newest first."""
        return sorted(witnesses, key=lambda witness: int(witness.alias.lstrip("w")))

    def _emit(self, document):
        self._say(json.dumps(document, indent=2))

    def _say(self, line):
        self._out(f"{line}\n")


def _salt(seed, alias):
    """A qb64 salt derived from the seed and this witness's name, so a pool repeats exactly.

    Hashed rather than used raw because ``kli init --salt`` wants a qualified 16-byte salt and a
    person's seed is a word. Different witnesses of one pool must not share a salt, or they would
    share an AID, so the alias goes into the hash.
    """
    raw = hashlib.blake2b(f"{seed}/{alias}".encode(), digest_size=16).digest()
    return Salter(raw=raw).qb64


def _stdout(text):
    sys.stdout.write(text)
