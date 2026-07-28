"""Thin, mockable WSGI-serving seam over waitress."""

from __future__ import annotations

import waitress


def serve(app, host, port):
    """Serve ``app`` on ``host:port`` until the process is stopped."""
    waitress.serve(app, host=host, port=port)
