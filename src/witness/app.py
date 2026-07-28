"""falcon WSGI application exposing the control-plane HTTP endpoints (unauthenticated in P0).

``/healthz`` is a liveness probe; ``/info`` reports the witness's identity. Both map a typed
:class:`~witness.errors.WitnessError` (e.g. the database being unreadable) to HTTP 503 with a
structured error body.
"""

from __future__ import annotations

import falcon

from .errors import WitnessError


class HealthResource:
    """GET /healthz — liveness probe backed by a read-only database open."""

    def __init__(self, reader):
        self._reader = reader

    def on_get(self, req, resp):
        try:
            resp.media = self._reader.health()
        except WitnessError as exc:
            resp.status = falcon.HTTP_503
            resp.media = {"status": "unavailable", "error": exc.to_dict()}


class InfoResource:
    """GET /info — the witness's own AID, alias, keripy version, and database path."""

    def __init__(self, reader):
        self._reader = reader

    def on_get(self, req, resp):
        try:
            resp.media = self._reader.info()
        except WitnessError as exc:
            resp.status = falcon.HTTP_503
            resp.media = exc.to_dict()


def make_app(reader):
    """Build the falcon application wired to ``reader``."""
    app = falcon.App()
    app.add_route("/healthz", HealthResource(reader))
    app.add_route("/info", InfoResource(reader))
    return app
