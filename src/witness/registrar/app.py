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
from urllib.parse import urlsplit

import falcon
import fiki

from ..app import _PROBLEM_JSON, RequestIdMiddleware
from ..errors import (RegistrarDenied, RegistrarInput, RegistrarTooLarge,
                      RegistrarUnauthenticated, WitnessError)

MAX_BODY = 8 * 1024 * 1024
MAX_NAME = 128
MAX_URL = 256
_SNAPSHOT = ("registry", "issuer", "digest", "chain", "kel")


def _body(req) -> bytes:
    length = req.content_length or 0
    if length > MAX_BODY:
        raise RegistrarTooLarge(f"The request is {length} bytes; I read at most {MAX_BODY}.",
                                args=[length, MAX_BODY])
    return req.bounded_stream.read(length) if length else b""


def _signer(req, body: bytes, max_age: int) -> str:
    """The AID that signed this request, having checked the signature covers the body."""
    try:
        verdict = fiki.verify_request(method=req.method, url=req.url, headers=req.headers,
                                      body=body or None, max_age=max_age)
    except fiki.FikiError as refused:
        raise RegistrarUnauthenticated(f"The request's signature did not verify: {refused}.")
    if body and "content-digest" not in verdict.covered:
        raise RegistrarUnauthenticated("The signature does not cover the request body.")
    return verdict.aid


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
    return {**{name: document[name] for name in ("registry", "issuer", "digest")}, **decoded}


def _callback(document: dict) -> str:
    callback = document.get("callback")
    parts = urlsplit(callback) if isinstance(callback, str) and len(callback) <= MAX_URL else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc or \
            set(document) != {"callback"}:
        raise RegistrarInput(f"A subscription names one http(s) callback of at most {MAX_URL} "
                             "characters.")
    return callback


class _Resource:
    def __init__(self, store, publishers: frozenset, max_age: int) -> None:
        self.store, self.publishers, self.max_age = store, publishers, max_age

    def _answer(self, req, resp, act) -> None:
        try:
            status, media = act()
        except WitnessError as refused:
            resp.status = falcon.util.code_to_http_status(refused.status)
            resp.content_type = _PROBLEM_JSON
            resp.set_header("Content-Language", "en")
            resp.media = refused.problem(instance=req.path,
                                         request_id=getattr(req.context, "request_id", None))
            return
        resp.status = falcon.util.code_to_http_status(status)
        if media is not None:
            resp.media = media


class Publication(_Resource):
    def on_post(self, req, resp) -> None:
        def act():
            body = _body(req)
            signer = _signer(req, body, self.max_age)
            if signer not in self.publishers:
                raise RegistrarDenied(f"{signer} is not a configured publisher.", args=[signer])
            snapshot = _snapshot(_object(body))
            return 200, {"digest": self.store.publish(**snapshot)}
        self._answer(req, resp, act)


class Subscription(_Resource):
    def on_post(self, req, resp) -> None:
        def act():
            body = _body(req)
            signer = _signer(req, body, self.max_age)
            self.store.subscribe(signer, _callback(_object(body)))
            return 200, {"subscriber": signer, "registrar": self.store.key().aid}
        self._answer(req, resp, act)

    def on_delete(self, req, resp) -> None:
        def act():
            signer = _signer(req, _body(req), self.max_age)
            if not self.store.unsubscribe(signer):
                from ..errors import RegistrarNoSubscription
                raise RegistrarNoSubscription(f"{signer} has no subscription here.", args=[signer])
            return 204, None
        self._answer(req, resp, act)


def make_registrar_app(store, *, publishers: frozenset, max_age: int) -> falcon.App:
    app = falcon.App(middleware=[RequestIdMiddleware()])
    app.resp_options.media_handlers[_PROBLEM_JSON] = falcon.media.JSONHandler()
    app.add_route("/v1/registrar/publication", Publication(store, publishers, max_age))
    app.add_route("/v1/registrar/subscription", Subscription(store, publishers, max_age))
    return app
