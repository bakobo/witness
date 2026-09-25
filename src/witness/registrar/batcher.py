"""The batch clock and the one seam its transport sits behind (@ed3dkgl5, @3m2eys6w)."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import fiki

logger = logging.getLogger(__name__)


class HttpTransport:
    """POST a signed batch to a subscriber's callback. The seam a webhook or SSE replaces."""

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def send(self, url: str, body: bytes, headers: dict) -> None:
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except urllib.error.HTTPError as refused:
            raise ConnectionError(f"{url} answered {refused.code}") from refused


def encode(batch, *, registrar: str, window_end: float) -> bytes:
    """A batch as JSON bytes: the Registrar's AID, number, window end and every head it carries."""
    return json.dumps({
        "registrar": registrar,
        "number": batch.number,
        "full": batch.full,
        "window_end": datetime.fromtimestamp(window_end, timezone.utc).isoformat(),
        "heads": [{**head, "chain": base64.b64encode(head["chain"]).decode(),
                   "kel": base64.b64encode(head["kel"]).decode()} for head in batch.heads],
    }, sort_keys=True).encode()


class Batcher:
    """Every window, compose, sign and send one batch to each subscriber."""

    def __init__(self, *, store, transport, window: float, clock=time.time) -> None:
        if not window > 0:
            raise ValueError("The batch window must be a positive number of seconds.")
        self.store, self.transport, self.window, self.clock = store, transport, window, clock
        self.key = store.key()

    def tick(self) -> int:
        """Send this window's batches; return how many subscribers received theirs."""
        delivered = 0
        now = self.clock()
        for aid, callback in self.store.subscribers():
            body = encode(self.store.compose(aid), registrar=self.key.aid, window_end=now)
            headers = fiki.sign_request(key=self.key, method="POST", url=callback, body=body)
            try:
                self.transport.send(callback, body, headers)
            except Exception as failure:  # the number is spent; the Observer will see a gap
                logger.warning("Batch to %s was not delivered: %s", callback, failure)
                continue
            delivered += 1
        return delivered

    def run(self, stop: threading.Event) -> None:
        """The send loop. ~7nq3 — a heartbeat batch every window is a temporary demo transport
        (Q-ZGF7); replace it with a webhook, SSE or better behind ``transport``."""
        while not stop.wait(self.window):
            self.tick()
