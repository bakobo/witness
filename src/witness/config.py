"""Control-plane configuration and argument parsing.

The one ``witness`` CLI uses a subcommand form; ``control-plane`` is the P0 subcommand.
Parsing fails closed: any malformed or missing argument raises a typed
:class:`~witness.errors.InvalidArguments` rather than exiting the process.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from .errors import InvalidArguments
from .supervisor import ProcessSpec

_MIN_PORT = 1
_MAX_PORT = 65535
#: Beside the keystore, so it shares the volume's lifetime and needs no extra mount.
_DEFAULT_TELEMETRY_PATH = "/usr/local/var/keri/telemetry"


@dataclass(frozen=True)
class ControlPlaneConfig:
    """Resolved configuration for the control-plane reader process."""

    name: str
    host: str
    port: int
    base: str = ""
    head_dir_path: str | None = None
    telemetry_path: str | None = None


@dataclass(frozen=True)
class RunnerConfig:
    """Resolved configuration for the witness runner process (``witness run``)."""

    name: str
    alias: str
    base: str
    passcode: str | None
    config_dir: str | None
    config_file: str | None
    tcp_port: int
    http_port: int
    telemetry_path: str


@dataclass(frozen=True)
class SupervisorConfig:
    """Resolved configuration for the in-image supervisor: the processes it runs, in order."""

    specs: tuple[ProcessSpec, ...]


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
    cp.add_argument(
        "--telemetry-path",
        default=_DEFAULT_TELEMETRY_PATH,
        help="Where the witness publishes its telemetry segment. Omit with --no-telemetry.",
    )
    cp.add_argument(
        "--no-telemetry",
        action="store_true",
        help="Serve without loop telemetry, for a witness started by stock `kli witness start`.",
    )
    sup = sub.add_parser(
        "supervise", help="Run the witness runner and the control plane in one container."
    )
    # Commands are opaque strings rather than structured flags so the image supplies them as CMD
    # arguments, and so the runner can move from `kli witness start` to `witness run` (@n5r2vq)
    # without a code change here.
    sup.add_argument(
        "--essential",
        required=True,
        help="The command whose exit ends the container (the witness runner).",
    )
    sup.add_argument(
        "--auxiliary",
        action="append",
        default=[],
        help="A command that is restarted if it exits, never taking the witness down. Repeatable.",
    )
    run = sub.add_parser("run", help="Run the keripy witness with in-loop telemetry.")
    run.add_argument("--name", default="witness", help="The witness keystore/database name.")
    run.add_argument("--alias", default=None, help="The hab alias. Defaults to --name.")
    run.add_argument("--base", default="", help="The keystore base subdirectory.")
    run.add_argument("--passcode", default=None, help="Keystore passcode, if it is encrypted.")
    run.add_argument("--config-dir", default=None, help="Configuration directory override.")
    run.add_argument("--config-file", default=None, help="Configuration filename override.")
    # keripy's own defaults, taken from `kli witness start`'s argparse rather than from
    # runWitness()'s signature, which has the two the other way round.
    run.add_argument("--http", default=5631, type=int, help="Witness HTTP port. Default 5631.")
    run.add_argument("--tcp", default=5632, type=int, help="Witness CESR/TCP port. Default 5632.")
    run.add_argument(
        "--telemetry-path",
        default=_DEFAULT_TELEMETRY_PATH,
        help=f"Where to publish the telemetry segment. Default {_DEFAULT_TELEMETRY_PATH}.",
    )
    return parser


def _port(value, flag):
    if not _MIN_PORT <= value <= _MAX_PORT:
        raise InvalidArguments(
            f"The {flag} value must be between {_MIN_PORT} and {_MAX_PORT}, but was {value}."
        )
    return value


def _runner_config(ns) -> RunnerConfig:
    return RunnerConfig(
        name=ns.name,
        alias=ns.alias if ns.alias is not None else ns.name,
        base=ns.base,
        passcode=ns.passcode,
        config_dir=ns.config_dir,
        config_file=ns.config_file,
        tcp_port=_port(ns.tcp, "--tcp"),
        http_port=_port(ns.http, "--http"),
        telemetry_path=ns.telemetry_path,
    )


def _control_plane_config(ns) -> ControlPlaneConfig:
    if not _MIN_PORT <= ns.port <= _MAX_PORT:
        raise InvalidArguments(
            f"The --port value must be between {_MIN_PORT} and {_MAX_PORT}, but was {ns.port}."
        )
    return ControlPlaneConfig(
        telemetry_path=None if ns.no_telemetry else ns.telemetry_path,
        name=ns.name,
        host=ns.host,
        port=ns.port,
        base=ns.base,
        head_dir_path=ns.head_dir_path,
    )


def _supervisor_config(ns) -> SupervisorConfig:
    specs = [ProcessSpec.from_command("essential", ns.essential, essential=True)]
    for index, command in enumerate(ns.auxiliary, start=1):
        specs.append(ProcessSpec.from_command(f"auxiliary-{index}", command, essential=False))
    return SupervisorConfig(specs=tuple(specs))


def parse_args(argv):
    """Parse ``argv`` into ``(subcommand, config)`` or raise InvalidArguments."""
    ns = _build_parser().parse_args(argv)
    if ns.subcommand == "supervise":
        return ns.subcommand, _supervisor_config(ns)
    if ns.subcommand == "run":
        return ns.subcommand, _runner_config(ns)
    return ns.subcommand, _control_plane_config(ns)
