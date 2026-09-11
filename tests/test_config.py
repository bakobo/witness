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
        name="testwit", host="0.0.0.0", port=5666, base="b1", head_dir_path="/tmp/hd",
        telemetry_path=config._DEFAULT_TELEMETRY_PATH,
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


def test_parses_the_supervise_subcommand():
    subcommand, cfg = config.parse_args(
        [
            "supervise",
            "--essential", "kli witness start --name testwit",
            "--auxiliary", "witness control-plane --name testwit --port 5621",
        ]
    )
    assert subcommand == "supervise"
    assert [spec.name for spec in cfg.specs] == ["essential", "auxiliary-1"]
    assert cfg.specs[0].argv == ("kli", "witness", "start", "--name", "testwit")
    assert cfg.specs[0].essential is True
    assert cfg.specs[1].essential is False


def test_supervise_accepts_repeated_auxiliaries():
    _subcommand, cfg = config.parse_args(
        ["supervise", "--essential", "a", "--auxiliary", "b", "--auxiliary", "c"]
    )
    assert [spec.name for spec in cfg.specs] == ["essential", "auxiliary-1", "auxiliary-2"]


def test_supervise_without_auxiliaries_is_allowed():
    _subcommand, cfg = config.parse_args(["supervise", "--essential", "a"])
    assert len(cfg.specs) == 1


def test_supervise_missing_essential_fails_closed():
    with pytest.raises(InvalidArguments):
        config.parse_args(["supervise", "--auxiliary", "b"])


def test_supervise_blank_command_fails_closed():
    with pytest.raises(InvalidArguments):
        config.parse_args(["supervise", "--essential", "   "])


def test_parses_the_run_subcommand_with_keripy_port_defaults():
    subcommand, cfg = config.parse_args(["run", "--name", "wit"])
    assert subcommand == "run"
    # kli's argparse defaults, not runWitness()'s signature, which has these reversed.
    assert (cfg.http_port, cfg.tcp_port) == (5631, 5632)
    assert cfg.alias == "wit", "alias defaults to name rather than to keripy's literal 'witness'"


def test_run_accepts_an_explicit_alias_and_ports():
    _subcommand, cfg = config.parse_args(
        ["run", "--name", "w", "--alias", "other", "--http", "1", "--tcp", "2"]
    )
    assert (cfg.alias, cfg.http_port, cfg.tcp_port) == ("other", 1, 2)


@pytest.mark.parametrize("flag", ["--http", "--tcp"])
def test_run_rejects_an_out_of_range_port(flag):
    with pytest.raises(InvalidArguments):
        config.parse_args(["run", "--name", "w", flag, "70000"])


def test_the_control_plane_can_be_told_the_witness_publishes_no_telemetry():
    """A witness started from stock `kli witness start` publishes none, and the control plane
    should serve the rest of its surface rather than refuse to start."""
    _subcommand, cfg = config.parse_args(
        ["control-plane", "--name", "w", "--port", "1", "--no-telemetry"]
    )
    assert cfg.telemetry_path is None


def test_pool_up_carries_the_verb_and_its_own_arguments():
    subcommand, cfg = config.parse_args(
        ["pool", "up", "--name", "lab", "--count", "7", "--image", "witness:test",
         "--base-port", "5700", "--seed", "merti", "--timeout", "30"]
    )
    assert subcommand == "pool"
    assert (cfg.verb, cfg.name, cfg.count, cfg.image) == ("up", "lab", 7, "witness:test")
    assert (cfg.base_port, cfg.seed, cfg.timeout) == (5700, "merti", 30.0)


def test_pool_up_defaults_its_image_to_the_environment(monkeypatch):
    """The same WITNESS_IMAGE the image oracles already take, because no floating tag exists."""
    monkeypatch.setenv("WITNESS_IMAGE", "ghcr.io/bakobo/witness@sha256:abc")
    _subcommand, cfg = config.parse_args(["pool", "up"])
    assert cfg.image == "ghcr.io/bakobo/witness@sha256:abc"


def test_a_pool_verb_that_takes_no_image_still_yields_a_whole_config():
    """Every verb folds into one PoolConfig, so the absent arguments have to default."""
    _subcommand, cfg = config.parse_args(["pool", "break", "--witness", "w2", "--mode", "pause"])
    assert (cfg.verb, cfg.witness, cfg.mode, cfg.name) == ("break", "w2", "pause", "default")
    assert (cfg.image, cfg.toad, cfg.all_pools, cfg.fmt) == (None, None, False, "json")


def test_the_pool_manifest_format_is_one_of_three():
    _subcommand, cfg = config.parse_args(["pool", "manifest", "--format", "heti", "--toad", "3"])
    assert (cfg.fmt, cfg.toad) == ("heti", 3)
    with pytest.raises(InvalidArguments):
        config.parse_args(["pool", "manifest", "--format", "yaml"])


def test_pool_down_can_be_told_to_sweep_everything():
    _subcommand, cfg = config.parse_args(["pool", "down", "--all"])
    assert cfg.all_pools is True
