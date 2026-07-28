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
