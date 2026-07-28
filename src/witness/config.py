"""Control-plane configuration and argument parsing.

The one ``witness`` CLI uses a subcommand form; ``control-plane`` is the P0 subcommand.
Parsing fails closed: any malformed or missing argument raises a typed
:class:`~witness.errors.InvalidArguments` rather than exiting the process.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from .errors import InvalidArguments

_MIN_PORT = 1
_MAX_PORT = 65535


@dataclass(frozen=True)
class ControlPlaneConfig:
    """Resolved configuration for the control-plane reader process."""

    name: str
    host: str
    port: int
    base: str = ""
    head_dir_path: str | None = None


class _RaisingParser(argparse.ArgumentParser):
    """An ArgumentParser that fails closed with a typed error instead of exiting."""

    def error(self, message):  # noqa: D102 - overrides argparse's process-exiting behavior
        raise InvalidArguments(f"The control-plane arguments are invalid: {message}.")


def _build_parser() -> _RaisingParser:
    parser = _RaisingParser(prog="witness")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    cp = sub.add_parser("control-plane", help="Run the read-only control-plane reader.")
    cp.add_argument("--name", required=True, help="The witness keystore/database name.")
    cp.add_argument("--base", default="", help="The keystore base subdirectory.")
    cp.add_argument(
        "--head-dir-path", default=None, help="The keystore head directory (keripy default if omitted)."
    )
    cp.add_argument("--host", default="127.0.0.1", help="The host/interface to bind.")
    cp.add_argument("--port", required=True, type=int, help="The TCP port to bind.")
    return parser


def parse_args(argv):
    """Parse ``argv`` into ``(subcommand, ControlPlaneConfig)`` or raise InvalidArguments."""
    ns = _build_parser().parse_args(argv)
    if not _MIN_PORT <= ns.port <= _MAX_PORT:
        raise InvalidArguments(
            f"The --port value must be between {_MIN_PORT} and {_MAX_PORT}, but was {ns.port}."
        )
    config = ControlPlaneConfig(
        name=ns.name,
        host=ns.host,
        port=ns.port,
        base=ns.base,
        head_dir_path=ns.head_dir_path,
    )
    return ns.subcommand, config
