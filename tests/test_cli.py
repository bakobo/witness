"""CLI wiring — main() parses args, builds a falcon app, and hands it to server.serve with the
configured host/port; and server.serve delegates to waitress. Both are exercised without binding
a socket by monkeypatching the serve seams."""

import falcon
import waitress

from witness import cli
from witness import server as server_mod


def test_main_parses_args_builds_app_and_serves(monkeypatch):
    captured = {}

    def fake_serve(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(server_mod, "serve", fake_serve)

    cli.main(["control-plane", "--name", "testwit", "--host", "127.0.0.1", "--port", "5621"])

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5621
    assert isinstance(captured["app"], falcon.App)


def test_serve_delegates_to_waitress(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        waitress, "serve", lambda app, host, port: calls.update(app=app, host=host, port=port)
    )
    sentinel = object()

    server_mod.serve(sentinel, "1.2.3.4", 9)

    assert calls == {"app": sentinel, "host": "1.2.3.4", "port": 9}


def test_main_dispatches_supervise_and_returns_its_exit_code(monkeypatch):
    from witness import supervisor as supervisor_mod

    seen = {}

    class FakeSupervisor:
        def __init__(self, specs):
            seen["specs"] = specs

        def install_signal_handlers(self):
            seen["handlers"] = True

        def run(self):
            return 4

    monkeypatch.setattr(supervisor_mod, "Supervisor", FakeSupervisor)

    assert cli.main(["supervise", "--essential", "kli witness start"]) == 4
    assert seen["handlers"] is True
    assert seen["specs"][0].argv == ("kli", "witness", "start")


def test_main_returns_zero_for_the_control_plane(monkeypatch):
    monkeypatch.setattr(server_mod, "serve", lambda app, host, port: None)

    assert cli.main(["control-plane", "--name", "w", "--port", "5621"]) == 0


def test_main_dispatches_run_and_returns_its_exit_code(monkeypatch):
    from witness import runner as runner_mod

    seen = {}

    class FakeRunner:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        def run(self):
            return 7

    monkeypatch.setattr(runner_mod, "WitnessRunner", FakeRunner)

    assert cli.main(["run", "--name", "wit"]) == 7
    assert seen["cfg"].name == "wit"


def test_the_control_plane_starts_metric_export(monkeypatch):
    from witness import metrics as metrics_mod

    seen = {}
    monkeypatch.setattr(
        metrics_mod,
        "configure",
        lambda reader, counter=None: seen.update(reader=reader, counter=counter),
    )
    monkeypatch.setattr(server_mod, "serve", lambda app, host, port: None)

    cli.main(["control-plane", "--name", "w", "--port", "5621"])

    assert seen["reader"] is not None
    assert seen["counter"] is not None, "export needs the request counter the app is counting into"
