"""The ``witness`` command-line entry point.

Wires the ``control-plane`` subcommand: parse arguments, build the read-only reader and the
falcon app over it, then serve. Serving is delegated to :mod:`witness.server` (a mockable seam).
"""

from __future__ import annotations

from . import config, server
from .app import make_app
from .reader import WitnessReader


def main(argv=None):
    """Parse arguments, build the reader and app, and serve the control plane."""
    _subcommand, cfg = config.parse_args(argv)
    reader = WitnessReader(cfg)
    app = make_app(reader)
    server.serve(app, cfg.host, cfg.port)
