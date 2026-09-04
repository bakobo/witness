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

from keri.app import Configer, Habery, HaberyDoer, Keeper, indirecting
from keri.cli.common import setupHby

from . import inloop, telemetry

#: hio's own default, and what ``directing.runController`` uses. Named here because the telemetry
#: doer measures lag against it: a pass that takes longer than a tock is a pass running behind.
TOCK = 0.03125


def open_habery(config):
    """Open the witness's keystore exactly as ``kli witness start`` does.

    The branch reads oddly and is deliberately faithful. ``aeid`` is ``None`` only when no
    keystore exists at all; a real ``kli init --nopasscode`` keystore reports ``''``, so the
    ordinary path is ``setupHby``. There is no head-directory override because ``setupHby`` opens
    its own ``Keeper`` without one — which is why ``kli witness start`` offers no such flag either,
    and why this does not invent one.
    """
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
        hby = self._open_habery(self._config)
        doers = self._build_doers(self._config, hby)
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
