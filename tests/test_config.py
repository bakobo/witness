"""Control-plane argument parsing — happy path plus fail-closed on bad or missing input."""

import pytest

from witness import config
from witness.errors import InvalidArguments


def test_parses_the_full_happy_path():
    subcommand, cfg = config.parse_args(
        [
            "control-plane",
            "--name", "testwit",
            "--base", "b1",
            "--head-dir-path", "/tmp/hd",
            "--host", "0.0.0.0",
            "--port", "5666",
        ]
    )
    assert subcommand == "control-plane"
    assert cfg == config.ControlPlaneConfig(
        name="testwit", host="0.0.0.0", port=5666, base="b1", head_dir_path="/tmp/hd"
    )


def test_applies_documented_defaults():
    _subcommand, cfg = config.parse_args(["control-plane", "--name", "w", "--port", "5621"])
    assert cfg.base == ""
    assert cfg.head_dir_path is None
    assert cfg.host == "127.0.0.1"


def test_missing_required_name_fails_closed():
    with pytest.raises(InvalidArguments):
        config.parse_args(["control-plane", "--port", "5621"])


def test_missing_required_port_fails_closed():
    with pytest.raises(InvalidArguments):
        config.parse_args(["control-plane", "--name", "w"])


def test_non_integer_port_fails_closed():
    with pytest.raises(InvalidArguments):
        config.parse_args(["control-plane", "--name", "w", "--port", "not-a-number"])


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_out_of_range_port_fails_closed(port):
    with pytest.raises(InvalidArguments):
        config.parse_args(["control-plane", "--name", "w", "--port", port])


def test_in_range_port_boundaries_are_accepted():
    _sub, low = config.parse_args(["control-plane", "--name", "w", "--port", "1"])
    _sub, high = config.parse_args(["control-plane", "--name", "w", "--port", "65535"])
    assert low.port == 1
    assert high.port == 65535
