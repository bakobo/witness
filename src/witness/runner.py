"""``witness run`` — the launcher (@n5r2vq).

Imports keripy as a library, assembles the stock ``setupWitness`` doers unchanged, and runs them
in a :class:`~witness.inloop.TimedDoist` alongside one telemetry doer. What the witness *does* is
entirely keripy's; what this adds is the ability to see it (@vxt7feoi).

The keystore setup mirrors ``keri.app.cli.commands.witness.start.runWitness``, which cannot be
reused directly because its last line is ``runController`` — the one line that has to change, so
the Doist can be ours. Everything above that line is reproduced rather than reinvented, and
``tests/test_runner.py`` guards the reproduction against an upstream bump.
"""

from __future__ import annotations

from collections import namedtuple

from keri.app import Configer, Habery, HaberyDoer, Keeper, indirecting, keeping
from keri.core.eventing import Kevery
from keri.cli.common import setupHby
from keri.db import basing

from . import escrows, inloop, paths, telemetry
from .errors import KeystoreLost

#: hio's own default, and what ``directing.runController`` uses. Named here because the telemetry
#: doer measures lag against it: a pass that takes longer than a tock is a pass running behind.
TOCK = 0.03125


_Probe = namedtuple("_Probe", "name base head_dir_path")


def _incepted(config, head_dir_path=None) -> bool:
    """Whether the database already holds a witness identity, read without creating one.

    ``paths.resolve`` first because a keripy read-only open of a MISSING database does not fail,
    it creates an empty one (~5s3e) — so "is there a database" has to be answered from the
    filesystem before anything opens it.
    """
    probe = _Probe(name=config.name, base=config.base, head_dir_path=head_dir_path)
    if paths.resolve(basing.Baser, probe) is None:
        return False
    rdb = basing.Baser(
        name=config.name, base=config.base, temp=False, headDirPath=head_dir_path, reopen=False
    )
    rdb.reopen(readonly=True)
    try:
        return any(True for _ in rdb.habs.getTopItemIter())
    finally:
        rdb.close()


def open_habery(config, *, resolve=paths.resolve, incepted=_incepted):
    """Open the witness's keystore exactly as ``kli witness start`` does, unless it is gone.

    The branch reads oddly and is deliberately faithful. ``aeid`` is ``None`` only when no
    keystore exists at all; a real ``kli init --nopasscode`` keystore reports ``''``, so the
    ordinary path is ``setupHby``. There is no head-directory override because ``setupHby`` opens
    its own ``Keeper`` without one — which is why ``kli witness start`` offers no such flag either,
    and why this does not invent one.

    Before any of that, the two stores are compared (~5dnx). A witness whose keystore has gone but
    whose database still holds its hab would otherwise start perfectly: keripy creates a fresh
    keystore, and the witness then serves the identity recorded in the database while signing with
    keys that identity does not name. Everything observable about it says healthy. The comparison
    has to happen HERE, before ``Keeper(reopen=True)``, because that call creates the keystore it
    was asked to open — so a probe made afterwards would always find one.
    """
    # keeping.Keeper rather than the module-level Keeper: the probe asks the CLASS where keripy
    # would put a keystore, which is a different question from which class opens it, and only the
    # second is a seam tests replace.
    if resolve(keeping.Keeper, _Probe(config.name, config.base, None)) is None and incepted(config):
        raise KeystoreLost(
            f"The witness database for {config.name!r} holds an identity, but there is no keystore "
            f"to sign for it. Starting would create a new keystore and serve the old AID with new "
            f"keys, which no validator could detect. A restore that copied the database without "
            f"the keystore is the usual cause; restore the keystore from the same backup."
        )
    keeper = Keeper(name=config.name, base=config.base, temp=False, reopen=True)
    aeid = keeper.gbls.get("aeid")
    keeper.close()  # release the LMDB env before Habery reopens the same keystore

    configer = None
    if config.config_file:
        configer = Configer(
            name=config.config_file, headDirPath=config.config_dir, temp=False,
            reopen=True, clear=False,
        )
    if aeid is None:
        return Habery(name=config.name, base=config.base, bran=config.passcode, cf=configer)
    return setupHby(name=config.name, base=config.base, bran=config.passcode, cf=configer)


def build_doers(config, hby, setup_witness=indirecting.setupWitness):
    """The stock witness doers, unchanged, with a HaberyDoer ahead of them."""
    doers = [HaberyDoer(habery=hby)]
    doers.extend(
        setup_witness(
            alias=config.alias, hby=hby, tcpPort=config.tcp_port, httpPort=config.http_port
        )
    )
    return doers


class WitnessRunner:
    """Assembles and runs the witness. Every keripy and filesystem seam is injectable, so the
    wiring is testable without a keystore or a socket."""

    def __init__(
        self,
        config,
        *,
        open_habery=open_habery,
        build_doers=build_doers,
        create_segment=telemetry.SegmentWriter.create,
        doist_factory=inloop.TimedDoist,
    ):
        self._config = config
        self._open_habery = open_habery
        self._build_doers = build_doers
        self._create_segment = create_segment
        self._doist_factory = doist_factory

    def run(self) -> int:
        # @znm5uppx. Set before setupWitness builds its Kevery, because keripy reads this off the
        # CLASS — which is keripy's own configuration idiom (Baser.MapSize has the same shape), so
        # this configures an unforked dependency rather than patching one. Sustained escrow depth
        # is the attacker's request rate times this number, and the measured cost is linear in
        # depth (docs/escrow-load.md).
        Kevery.TimeoutQNF = self._config.escrow_timeout
        hby = self._open_habery(self._config)
        doers = self._build_doers(self._config, hby)
        # @zj3h2pzh. Substituted after setupWitness has built its doers and before the Doist
        # enters, which is the only window in which WitnessStart.doers is both complete and not
        # yet read.
        escrows.pace(doers, self._config.escrow_interval)
        writer, telemetry_doer = self._open_telemetry(doers)
        doers.append(telemetry_doer)
        doist = self._doist_factory(limit=0.0, tock=TOCK, real=True, doers=doers, sink=writer)
        doist.do()
        return 0

    def _open_telemetry(self, doers):
        """Create the segment for ``doers`` and the doer that publishes into it.

        Returns both rather than stashing the writer on self, because the writer is needed by the
        Doist as its sink and by the doer as its target — two consumers, so making one of them
        reach into instance state set by a method that looks like a factory hides the dependency.
        """
        # The telemetry doer is itself timed, so it must be in the name table before the segment
        # is created — hence naming a list that already includes it.
        names = inloop.TimedDoist.names_for(doers) + ["TelemetryDoer"]
        writer = self._create_segment(self._config.telemetry_path, names)
        return writer, inloop.TelemetryDoer(writer=writer, tock=TOCK)
