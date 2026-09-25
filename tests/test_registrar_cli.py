"""`witness registrar`: its configuration, its announcement, and its serving seams."""

import json
import threading

import falcon
import pytest

from witness import cli, config, server as server_mod
from witness.errors import InvalidArguments
from witness.registrar import runner as registrar_runner

PUBLISHER = "B" + "p" * 43


def test_parses_the_registrar_with_its_herd_privacy_default_window(tmp_path):
    subcommand, cfg = config.parse_args(["registrar", "--store", str(tmp_path), "--port", "7700",
                                         "--publisher", PUBLISHER])
    assert subcommand == "registrar"
    assert cfg.store == str(tmp_path)
    assert (cfg.host, cfg.port) == ("127.0.0.1", 7700)
    assert cfg.publishers == (PUBLISHER,)
    assert cfg.window == config.REGISTRAR_DEFAULT_WINDOW
    assert cfg.window >= 60, "herd privacy: minutes, not seconds"
    assert cfg.max_age == 60


def test_the_window_and_several_publishers_are_configuration(tmp_path):
    _, cfg = config.parse_args(["registrar", "--store", str(tmp_path), "--port", "1",
                                "--publisher", PUBLISHER, "--publisher", "B" + "q" * 43,
                                "--window", "5", "--max-age", "30", "--host", "0.0.0.0"])
    assert cfg.window == 5.0
    assert cfg.publishers == (PUBLISHER, "B" + "q" * 43)
    assert (cfg.max_age, cfg.host) == (30, "0.0.0.0")


@pytest.mark.parametrize("extra", [["--window", "0"], ["--window", "-1"], ["--max-age", "0"],
                                   ["--publisher", "not-an-aid"]])
def test_bad_registrar_arguments_fail_closed(tmp_path, extra):
    argv = ["registrar", "--store", str(tmp_path), "--port", "1", "--publisher", PUBLISHER]
    with pytest.raises(InvalidArguments):
        config.parse_args(argv + extra)


def test_a_registrar_needs_at_least_one_publisher(tmp_path):
    with pytest.raises(InvalidArguments):
        config.parse_args(["registrar", "--store", str(tmp_path), "--port", "1"])


def test_main_announces_starts_the_batch_clock_and_serves(tmp_path, monkeypatch, capsys):
    served, started = {}, []
    monkeypatch.setattr(server_mod, "serve",
                        lambda app, host, port: served.update(app=app, host=host, port=port))
    real_run = registrar_runner.Batcher.run

    def run(self, stop):
        started.append(self.window)
        stop.set()
        return real_run(self, stop)

    monkeypatch.setattr(registrar_runner.Batcher, "run", run)
    code = cli.main(["registrar", "--store", str(tmp_path), "--port", "7701",
                     "--publisher", PUBLISHER, "--window", "5"])
    assert code == 0
    assert isinstance(served["app"], falcon.App)
    assert (served["host"], served["port"]) == ("127.0.0.1", 7701)
    announced = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert announced["url"] == "http://127.0.0.1:7701"
    assert announced["aid"].startswith("B")
    registrar_runner.join_batcher(timeout=5)
    assert started == [5.0]
    assert not any(thread.name == "registrar-batcher" and thread.is_alive()
                   for thread in threading.enumerate())


def test_stopping_a_batcher_that_never_started_is_harmless(monkeypatch):
    monkeypatch.setattr(registrar_runner, "_BATCHER", {})
    registrar_runner.join_batcher(timeout=1)
