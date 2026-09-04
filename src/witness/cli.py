"""The ``witness`` command-line entry point.

Wires two subcommands. ``control-plane`` parses arguments, builds the read-only reader and the
falcon app over it, then serves; serving is delegated to :mod:`witness.server` (a mockable seam).
``supervise`` runs the witness runner and the control plane together inside one container
(@a24p3kbw) and returns the exit code the container should carry. ``run`` is the launcher
(@n5r2vq): stock keripy witness doers plus in-loop telemetry.
"""

from __future__ import annotations

from . import config, metrics, runner, server, supervisor
from .app import RequestCounter, make_app
from .reader import WitnessReader


def main(argv=None) -> int:
    """Dispatch the requested subcommand and return its process exit code."""
    subcommand, cfg = config.parse_args(argv)
    if subcommand == "run":
        return runner.WitnessRunner(cfg).run()
    if subcommand == "supervise":
        sup = supervisor.Supervisor(cfg.specs)
        sup.install_signal_handlers()
        return sup.run()
    reader = WitnessReader(cfg)
    # Held for the process's lifetime rather than discarded: the PeriodicExportingMetricReader
    # inside it owns the background thread that does the exporting. Returns None, and costs
    # nothing, when no OTLP collector is configured (@wea6qjmk).
    counter = RequestCounter()
    app = make_app(reader, counter=counter)
    _metrics = metrics.configure(  # noqa: F841 - kept alive deliberately
        reader, counter=counter
    )
    server.serve(app, cfg.host, cfg.port)
    return 0
