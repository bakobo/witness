"""The control-plane HTTP surface (@zzbdxa).

Every path is ``/v1/witness/<noun>``, per dev/standards/url-design.md: the version governs the
meaning of everything after it, the component names the family, and the nouns are singular things
it makes sense to GET. Errors are RFC 9457 problem+json, per dev/standards/http-errors.md, with
the status taken from the error's own code prefix rather than chosen at the call site.

Health deserves a note. ops.md §7 asks that a black-box probe "prove the service is working, not
that a port is open," and the earlier ``/healthz`` proved only that the witness's database could
be opened — which stays true of a witness whose hio loop has wedged, the failure mode that has
actually happened. So health now also asks the telemetry segment whether the loop is still
ticking, and reports unhealthy when it has stopped. That is the difference between a probe that
would have caught a wedge and one that would have sat there green.
"""

from __future__ import annotations

import uuid

import falcon

from .errors import WitnessError

_PROBLEM_JSON = "application/problem+json"
_REQUEST_ID_HEADER = "Bakobo-Request-Id"


class RequestIdMiddleware:
    """Assign every request a correlation id, echoed in a header and in any error envelope."""

    def process_request(self, req, resp):
        req.context.request_id = req.get_header(_REQUEST_ID_HEADER) or uuid.uuid4().hex

    def process_response(self, req, resp, resource, req_succeeded):
        resp.set_header(_REQUEST_ID_HEADER, getattr(req.context, "request_id", ""))


class Endpoint:
    """One GET endpoint over a callable, with uniform typed-error rendering.

    Uniform on purpose: sibling endpoints disagreeing about where an error lives is the
    inconsistency dev/standards/error-handling.md names in its rubric, and it is precisely what
    @zzbdxa found between the old ``/healthz`` and ``/info``.
    """

    def __init__(self, produce):
        self._produce = produce

    def on_get(self, req, resp, **params):
        try:
            resp.media = self._produce(**params)
        except WitnessError as exc:
            resp.status = falcon.util.code_to_http_status(exc.status)
            resp.content_type = _PROBLEM_JSON
            resp.media = exc.problem(
                instance=req.path, request_id=getattr(req.context, "request_id", None)
            )
            if exc.retryable:
                # The standard requires a Retry-After on e.state.pending; sending it on every
                # retryable failure is the same promise, made everywhere it is true.
                resp.set_header("Retry-After", "5")


def make_app(reader):
    """Build the falcon application wired to ``reader``."""
    app = falcon.App(middleware=[RequestIdMiddleware()])
    # falcon serializes resp.media only for media types it has a handler for, so setting the
    # RFC 9457 content type without registering it turns every error into a 415 — the error
    # about the error, which is the least useful response there is.
    app.resp_options.media_handlers[_PROBLEM_JSON] = falcon.media.JSONHandler()
    app.add_route("/v1/witness/health", Endpoint(reader.health))
    app.add_route("/v1/witness/identity", Endpoint(reader.identity))
    app.add_route("/v1/witness/version", Endpoint(reader.version))
    app.add_route("/v1/witness/loop", Endpoint(reader.loop))
    app.add_route("/v1/witness/escrow", Endpoint(reader.escrow))
    app.add_route("/v1/witness/database", Endpoint(reader.database))
    app.add_route("/v1/witness/process", Endpoint(reader.process))
    app.add_route("/v1/witness/controller", Endpoint(reader.controllers))
    app.add_route("/v1/witness/controller/{aid}", Endpoint(reader.controller))
    return app
