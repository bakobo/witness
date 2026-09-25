"""`witness registrar`: open the store, announce, run the batch clock, serve (@46t5otqe)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .. import server
from .app import make_registrar_app
from .batcher import Batcher, HttpTransport
from .store import RegistrarStore

_BATCHER: dict = {}
"""The running batch thread and its stop event, so a test (or a supervisor) can end it."""


def join_batcher(timeout: float) -> None:
    """Stop the batch clock and wait for its thread."""
    if _BATCHER:
        _BATCHER["stop"].set()
        _BATCHER["thread"].join(timeout=timeout)


def run(cfg) -> int:
    store = RegistrarStore(Path(cfg.store) / "registrar.sqlite3")
    batcher = Batcher(store=store, transport=HttpTransport(timeout=cfg.max_age),
                      window=cfg.window)
    # One line on stdout, for whoever started this process: where to reach it, and the AID its
    # batches are signed by, which an Observer pins.
    print(json.dumps({"aid": batcher.key.aid, "url": f"http://{cfg.host}:{cfg.port}"}),
          flush=True)
    stop = threading.Event()
    thread = threading.Thread(target=batcher.run, args=(stop,), name="registrar-batcher",
                              daemon=True)
    _BATCHER.update(stop=stop, thread=thread)
    thread.start()
    try:
        server.serve(make_registrar_app(store, publishers=frozenset(cfg.publishers),
                                        max_age=cfg.max_age), cfg.host, cfg.port)
    finally:
        join_batcher(timeout=cfg.window + 1)
        store.close()
    return 0
