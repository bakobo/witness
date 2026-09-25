"""The batch clock and the one seam its transport sits behind (@ed3dkgl5, @3m2eys6w)."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import fiki

from ..errors import RegistrarCallbackRefused

logger = logging.getLogger(__name__)

MAX_WORKERS = 32
"""Deliveries in flight at once; the cap on subscriptions bounds how many there can be."""


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


class CallbackPolicy:
    """Which callback destinations the Registrar will connect to.

    Any signer may subscribe, so a callback is attacker-chosen: without a policy the Registrar
    would POST to its own loopback, a link-local metadata service or the issuer's private
    network on request. Only globally routable addresses are allowed by default. An operator
    allows more by host name or CIDR (the demo's Observer is on loopback). The check resolves the
    host and returns the address to connect to, so the address checked is the address used.
    """

    def __init__(self, allow=(), *, resolve=_resolve) -> None:
        self.hosts = frozenset(entry for entry in allow if "/" not in entry
                               and not _is_address(entry))
        self.networks = tuple(ipaddress.ip_network(entry, strict=False) for entry in allow
                              if "/" in entry or _is_address(entry))
        self.resolve = resolve

    def check(self, url: str) -> str:
        host = urlsplit(url).hostname or ""
        try:
            addresses = [ipaddress.ip_address(host)] if _is_address(host) else \
                [ipaddress.ip_address(found) for found in self.resolve(host)]
        except (OSError, ValueError):
            addresses = []
        if not addresses:
            raise RegistrarCallbackRefused(f"The callback host {host!r} does not resolve.",
                                           args=[host])
        allowed_name = host in self.hosts
        for address in addresses:
            if not (allowed_name or address.is_global
                    or any(address in network for network in self.networks)):
                raise RegistrarCallbackRefused(
                    f"The callback {host!r} resolves to {address}, which is not a public "
                    "address and is not allowed by this Registrar's configuration.",
                    args=[host, str(address)])
        return str(addresses[0])


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


class _PinnedHTTP(http.client.HTTPConnection):
    """An HTTP connection to the vetted address, whatever the URL's host resolves to later."""

    def __init__(self, host, port, *, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    default_port = 443

    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock,
                                                             server_hostname=self.host)


class HttpTransport:
    """POST a signed batch to a subscriber's callback. The seam a webhook or SSE replaces.

    Redirects are never followed: a 3xx is a failed delivery, like any other non-2xx.
    """

    def __init__(self, *, timeout: float, policy: CallbackPolicy | None = None) -> None:
        self.timeout = timeout
        self.policy = policy or CallbackPolicy()

    def send(self, url: str, body: bytes, headers: dict) -> None:
        address = self.policy.check(url)
        parts = urlsplit(url)
        kind = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        connection = kind(parts.hostname, parts.port, address=address, timeout=self.timeout)
        try:
            connection.request("POST", parts.path or "/", body=body,
                               headers={**headers, "Content-Type": "application/json"})
            status = connection.getresponse().status
        finally:
            connection.close()
        if not 200 <= status < 300:
            raise ConnectionError(f"{url} answered {status}")


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
    """Every window, compose, sign and send one batch to each subscriber, all at once."""

    def __init__(self, *, store, transport, window: float, clock=time.time) -> None:
        if not window > 0:
            raise ValueError("The batch window must be a positive number of seconds.")
        timeout = getattr(transport, "timeout", None)
        if timeout is not None and not timeout <= window / 2:
            raise ValueError(f"The delivery timeout ({timeout} s) must be at most half the batch "
                             f"window ({window} s), so one slow callback cannot run into the "
                             "next window.")
        self.store, self.transport, self.window, self.clock = store, transport, window, clock
        self.key = store.key()

    def _prepare(self, now: float) -> list[tuple[str, bytes, dict]]:
        prepared = []
        for aid, callback in self.store.subscribers():
            try:
                body = encode(self.store.compose(aid), registrar=self.key.aid, window_end=now)
            except Exception as failure:  # e.g. unsubscribed since the list was read
                logger.warning("No batch for %s this window: %s", aid, failure)
                continue
            headers = fiki.sign_request(key=self.key, method="POST", url=callback, body=body)
            prepared.append((callback, body, headers))
        return prepared

    def _deliver(self, delivery) -> bool:
        callback, body, headers = delivery
        try:
            self.transport.send(callback, body, headers)
        except Exception as failure:  # the number is spent; the Observer will see a gap
            logger.warning("Batch to %s was not delivered: %s", callback, failure)
            return False
        return True

    def tick(self) -> int:
        """Send this window's batches concurrently; return how many were delivered."""
        prepared = self._prepare(self.clock())
        if not prepared:
            return 0
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(prepared))) as pool:
            return sum(pool.map(self._deliver, prepared))

    def run(self, stop: threading.Event) -> None:
        """The send loop. ~7nq3 — a heartbeat batch every window is a temporary demo transport
        (Q-ZGF7); replace it with a webhook, SSE or better behind ``transport``."""
        while not stop.wait(self.window):
            try:
                self.tick()
            except Exception:  # one bad window must never end batching for everyone
                logger.exception("A batch window failed; the next one will run.")
