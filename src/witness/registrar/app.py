"""The Registrar's HTTP surface: publish, subscribe, unsubscribe — and nothing to read (@ed3dkgl5).

Every request is RFC 9421-signed via fiki (@s6v3qm), with the body's content digest covered.
A publication must come from a configured publisher (@5m2m4ozz); a subscription may come from
any signer, whose AID becomes the subscription's identity. There is deliberately no GET route:
no verification anywhere can cause Registrar traffic if there is nothing to ask.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from urllib.parse import urlsplit

import falcon
import fiki
import http_sfv
from fiki.messages import DEFAULT_SKEW

from ..app import _PROBLEM_JSON, RequestIdMiddleware
from ..errors import (RegistrarDenied, RegistrarInput, RegistrarResolverTimeout,
                      RegistrarTooLarge, RegistrarUnauthenticated, WitnessError)
from .batcher import CallbackPolicy, _within
from .store import Sighting

MAX_BODY = 8 * 1024 * 1024
MAX_NAME = 128
MAX_URL = 256
DEFAULT_MAX_SUBSCRIPTIONS = 64
DEFAULT_MAX_SIGHTINGS = 4096
DEFAULT_MAX_SIGHTINGS_PER_SIGNER = 32
DEFAULT_ADMISSION_TIMEOUT = 5.0
MAX_ADMISSIONS = 16
"""Callback resolutions alive at once at subscription time, abandoned ones included."""
SKEW = DEFAULT_SKEW
"""The clock skew fiki tolerates, passed to it explicitly, so the replay window below and the
verifier's acceptance window are one number rather than two copies that can drift."""
_SNAPSHOT = ("registry", "issuer", "digest", "chain", "kel")


def _body(req) -> bytes:
    length = req.content_length or 0
    if length > MAX_BODY:
        raise RegistrarTooLarge(f"The request is {length} bytes; I read at most {MAX_BODY}.",
                                args=[length, MAX_BODY])
    return req.bounded_stream.read(length) if length else b""


def _signer(req, body: bytes, max_age: int, *, clock=time.time) -> tuple[str, int]:
    """The AID that signed this request, and the instant it was judged at.

    The clock is read ONCE: that instant is what the verifier judges freshness at and what the
    replay store prunes at, so a sighting can never be pruned while the verifier would still
    accept its request.
    """
    now = int(clock())
    try:
        verdict = fiki.verify_request(method=req.method, url=req.url, headers=req.headers,
                                      body=body or None, max_age=max_age, skew=SKEW, now=now)
    except fiki.FikiError as refused:
        raise RegistrarUnauthenticated(f"The request's signature did not verify: {refused}.")
    if body and "content-digest" not in verdict.covered:
        raise RegistrarUnauthenticated("The signature does not cover the request body.")
    return verdict.aid, now


def _signed_parts(req) -> tuple[int, bytes]:
    """The verified signature's ``created`` time and its decoded bytes.

    fiki has just verified this request, and it accepts exactly one signature, so each header is a
    one-entry RFC 8941 dictionary. The label is not signed and is ignored here: two requests that
    differ only in their label are the same request.
    """
    inputs, signatures = http_sfv.Dictionary(), http_sfv.Dictionary()
    inputs.parse(req.get_header("Signature-Input").encode())
    signatures.parse(req.get_header("Signature").encode())
    (entry,) = inputs.values()
    (value,) = signatures.values()
    return int(entry.params.get("created", 0)), bytes(value.value)


def _object(body: bytes) -> dict:
    try:
        document = json.loads(body)
    except ValueError:
        raise RegistrarInput("The request body is not JSON.") from None
    if not isinstance(document, dict):
        raise RegistrarInput("The request body is not a JSON object.")
    return document


def _snapshot(document: dict) -> dict:
    if set(document) != set(_SNAPSHOT):
        raise RegistrarInput(f"A publication names exactly {', '.join(_SNAPSHOT)}.")
    for name in ("registry", "issuer", "digest"):
        value = document[name]
        if not isinstance(value, str) or not 0 < len(value) <= MAX_NAME:
            raise RegistrarInput(f"{name} must be 1 to {MAX_NAME} characters of text.",
                                 args=[name])
    decoded = {}
    for name in ("chain", "kel"):
        try:
            decoded[name] = base64.b64decode(document[name], validate=True)
        except (TypeError, binascii.Error):
            raise RegistrarInput(f"{name} must be base64 text.", args=[name]) from None
        if not decoded[name]:  # an empty chain would be a prefix of every later one
            raise RegistrarInput(f"{name} must not be empty.", args=[name])
    return {**{name: document[name] for name in ("registry", "issuer", "digest")}, **decoded}


def _is_http_url(text: str) -> bool:
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError for a port outside 0-65535
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:  # a malformed authority such as http://[::1
        return False


_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _subscription(document: dict) -> tuple[str, str]:
    """A subscription's callback and nonce; every batch for it will carry the nonce
    (@jpag4sof)."""
    callback, nonce = document.get("callback"), document.get("nonce")
    if set(document) != {"callback", "nonce"} or not isinstance(callback, str) or \
            len(callback) > MAX_URL or not _is_http_url(callback) or \
            not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise RegistrarInput(f"A subscription names one http(s) callback of at most {MAX_URL} "
                             "characters and a nonce of 16 to 64 base64url characters.")
    return callback, nonce


class _Resource:
    def __init__(self, store, publishers: frozenset, max_age: int, *, callbacks=None,
                 max_subscriptions: int = DEFAULT_MAX_SUBSCRIPTIONS, clock=time.time,
                 max_sightings: int = DEFAULT_MAX_SIGHTINGS,
                 max_sightings_per_signer: int = DEFAULT_MAX_SIGHTINGS_PER_SIGNER,
                 admission_timeout: float = DEFAULT_ADMISSION_TIMEOUT) -> None:
        self.store, self.publishers, self.max_age, self.clock = store, publishers, max_age, clock
        self.callbacks = callbacks or CallbackPolicy()
        self.max_subscriptions = max_subscriptions
        self.max_sightings, self.max_sightings_per_signer = max_sightings, max_sightings_per_signer
        self.admission_timeout = admission_timeout
        self._admissions: list[threading.Thread] = []
        self._reserved = 0
        self._admissions_lock = threading.Lock()

    def _sighting(self, req, now: int) -> Sighting:
        created, signature = _signed_parts(req)
        return Sighting(created, signature, now, self.max_age + SKEW,
                        per_signer=self.max_sightings_per_signer, total=self.max_sightings)

    def _admit(self, callback: str) -> None:
        """The callback policy, under a deadline, with abandoned resolutions capped.

        Resolution is synchronous and a hostile resolver can stall it, so it runs on a daemon
        thread the request waits for at most ``admission_timeout``, and no more than
        MAX_ADMISSIONS such threads may be alive at once.
        """
        with self._admissions_lock:  # reserve a slot; the wait happens outside the lock
            self._admissions = [thread for thread in self._admissions if thread.is_alive()]
            if len(self._admissions) + self._reserved >= MAX_ADMISSIONS:
                raise RegistrarResolverTimeout("Too many callback resolutions are pending.")
            self._reserved += 1

        def track(thread):
            with self._admissions_lock:
                self._admissions.append(thread)
                self._reserved -= 1

        try:
            _within(time.monotonic() + self.admission_timeout,
                    lambda: self.callbacks.check(callback), f"resolving {callback}", track=track)
        except TimeoutError:
            raise RegistrarResolverTimeout(
                f"The callback host did not resolve within {self.admission_timeout} s.",
                args=[callback]) from None

    def _answer(self, req, resp, act) -> None:
        try:
            status, media = act()
        except WitnessError as refused:
            resp.status = falcon.util.code_to_http_status(refused.status)
            resp.content_type = _PROBLEM_JSON
            resp.set_header("Content-Language", "en")
            resp.media = refused.problem(instance=req.path,
                                         request_id=getattr(req.context, "request_id", None))
            if refused.retryable:  # the same promise the control plane makes (app.py)
                resp.set_header("Retry-After", "5")
            return
        resp.status = falcon.util.code_to_http_status(status)
        if media is not None:
            resp.media = media


class Publication(_Resource):
    def on_post(self, req, resp) -> None:
        def act():
            body = _body(req)
            signer, _ = _signer(req, body, self.max_age, clock=self.clock)
            if signer not in self.publishers:
                raise RegistrarDenied(f"{signer} is not a configured publisher.", args=[signer])
            snapshot = _snapshot(_object(body))
            return 200, {"digest": self.store.publish(**snapshot)}
        self._answer(req, resp, act)


class Subscription(_Resource):
    def on_post(self, req, resp) -> None:
        def act():
            body = _body(req)
            signer, now = _signer(req, body, self.max_age, clock=self.clock)
            callback, nonce = _subscription(_object(body))
            self._admit(callback)  # refused here too, not only at delivery
            # The replay record and the subscription commit together, so a refused request
            # leaves nothing behind and its retry is not mistaken for a replay.
            self.store.subscribe(signer, callback, nonce, limit=self.max_subscriptions,
                                 sighting=self._sighting(req, now))
            return 200, {"subscriber": signer, "registrar": self.store.key().aid}
        self._answer(req, resp, act)

    def on_delete(self, req, resp) -> None:
        def act():
            signer, now = _signer(req, _body(req), self.max_age, clock=self.clock)
            self.store.unsubscribe(signer, sighting=self._sighting(req, now))
            return 204, None
        self._answer(req, resp, act)


def make_registrar_app(store, *, publishers: frozenset, max_age: int, callbacks=None,
                       max_subscriptions: int = DEFAULT_MAX_SUBSCRIPTIONS,
                       clock=time.time, **bounds) -> falcon.App:
    """Publications are exempt from the replay check on purpose: an exact repeat is already
    re-acknowledged and anything older is refused as stale, so a replay changes nothing, and a
    publisher retrying one snapshot (signatures are deterministic) must not be refused for it."""
    app = falcon.App(middleware=[RequestIdMiddleware()])
    app.resp_options.media_handlers[_PROBLEM_JSON] = falcon.media.JSONHandler()
    options = dict(callbacks=callbacks, max_subscriptions=max_subscriptions, clock=clock,
                   **bounds)
    app.add_route("/v1/registrar/publication", Publication(store, publishers, max_age, **options))
    app.add_route("/v1/registrar/subscription",
                  Subscription(store, publishers, max_age, **options))
    return app
