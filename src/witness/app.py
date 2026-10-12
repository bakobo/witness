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

import time
import uuid

import falcon
import fiki

from .accesslog import AccessLog
from .errors import ControlDenied, ControlUnauthenticated, WitnessError

PROBLEM_JSON = "application/problem+json"
_REQUEST_ID_HEADER = "Bakobo-Request-Id"
#: A caller-supplied correlation id is echoed into a response header and into the error body, so
#: it is attacker-controlled data on two output paths. Bounded and character-restricted rather
#: than trusted: an unbounded value invites header stuffing and log-line forgery, and a caller
#: with a genuine id has no need of newlines or 400 characters to express it.
_REQUEST_ID_MAX = 64
_REQUEST_ID_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def _acceptable_request_id(value):
    return (
        bool(value)
        and len(value) <= _REQUEST_ID_MAX
        and all(character in _REQUEST_ID_ALLOWED for character in value)
    )


class RequestCounter:
    """Counts requests by route and outcome, for the metrics the deployment asked for.

    Kept here rather than in metrics.py because the count has to be taken where requests actually
    arrive, and kept as plain in-process state because these are the control plane's OWN numbers —
    unlike every other series, which reads through to LMDB or the telemetry segment at export time
    and so needs no shadow copy. Labelled by falcon's URI TEMPLATE, never the raw path: a counter
    keyed by /v1/witness/controller/<aid> would mint a new series per controller, which is the
    classic way to make a metrics backend unusable.
    """

    def __init__(self):
        self.counts = {}

    def process_response(self, req, resp, resource, req_succeeded):
        route = req.uri_template or "unmatched"
        status = int(str(resp.status)[:3])
        key = (route, status)
        self.counts[key] = self.counts.get(key, 0) + 1


class RequestIdMiddleware:
    """Assign every request a correlation id, echoed in a header and in any error envelope."""

    def process_request(self, req, resp):
        supplied = req.get_header(_REQUEST_ID_HEADER)
        req.context.request_id = (
            supplied if _acceptable_request_id(supplied) else uuid.uuid4().hex
        )

    def process_response(self, req, resp, resource, req_succeeded):
        resp.set_header(_REQUEST_ID_HEADER, getattr(req.context, "request_id", ""))


DEFAULT_MAX_AGE = 60
"""How old, in seconds, a signed read may be: the Registrar's window, for one number (@34sgjmxr)."""


class OperatorGuard:
    """Admit a request only if an operator of this witness signed it (@34sgjmxr, @s6v3qm).

    No replay store: a GET changes nothing, so a replay inside ``max_age`` discloses state only to
    someone who already captured the request, and keeping one would end @b2y5nr's statelessness.
    """

    def __init__(self, operators, *, max_age=DEFAULT_MAX_AGE, authorities=(), clock=time.time):
        self._operators = frozenset(operators)
        self._max_age = max_age
        # Empty means the check is declined, which fiki spells None. @34sgjmxr: the authority a
        # caller signs is the address it dialled, which a tunnel makes unknowable here.
        self._authorities = tuple(authorities) or None
        self._clock = clock

    def check(self, req):
        """Admit or raise, leaving the verdict on ``req.context`` for the request log (@79mupg4t).

        ``signer`` is set only once fiki has verified the signature, so a refused request never
        carries an AID it merely claimed.
        """
        if not self._operators:
            req.context.outcome = "denied"
            raise ControlDenied(
                "This control plane has no operator configured, so it answers this read for "
                "nobody. Name one with --operator or WITNESS_OPERATORS."
            )
        try:
            # No body is passed: a read has none, and fiki refuses one that a request announces
            # without its signature covering it.
            verdict = fiki.verify_request(
                method=req.method, url=req.url, headers=req.headers, max_age=self._max_age,
                now=int(self._clock()), authorities=self._authorities,
            )
        except fiki.FikiError as refused:
            req.context.outcome = "unauthenticated"
            raise ControlUnauthenticated(
                f"The request's signature did not verify: {refused}."
            ) from None
        req.context.signer = verdict.aid
        if verdict.aid not in self._operators:
            req.context.outcome = "denied"
            raise ControlDenied(f"{verdict.aid} is not an operator of this witness.",
                                args=[verdict.aid])
        req.context.outcome = "admitted"


class Endpoint:
    """One GET endpoint over a callable, with uniform typed-error rendering.

    Uniform on purpose: sibling endpoints disagreeing about where an error lives is the
    inconsistency dev/standards/error-handling.md names in its rubric, and it is precisely what
    @zzbdxa found between the old ``/healthz`` and ``/info``.
    """

    def __init__(self, produce, guard=None):
        self._produce = produce
        self._guard = guard

    def on_get(self, req, resp, **params):
        try:
            if self._guard is None:
                req.context.outcome = "open"
            else:
                self._guard.check(req)
            resp.media = self._produce(**params)
        except WitnessError as exc:
            req.context.error_type = exc.code
            resp.status = falcon.util.code_to_http_status(exc.status)
            resp.content_type = PROBLEM_JSON
            # http-errors.md's worked examples carry Content-Language, because `detail` is prose
            # and a client that localises needs to know what it just received.
            resp.set_header("Content-Language", "en")
            resp.media = exc.problem(
                instance=req.path, request_id=getattr(req.context, "request_id", None)
            )
            if exc.retryable:
                # The standard requires a Retry-After on e.state.pending; sending it on every
                # retryable failure is the same promise, made everywhere it is true.
                resp.set_header("Retry-After", "5")


def make_app(reader, counter=None, *, operators=(), max_age=DEFAULT_MAX_AGE, authorities=(),
             clock=time.time, access_log=None):
    """Build the falcon application wired to ``reader``.

    Five nouns answer anyone and six only a signing operator (@34sgjmxr). With no ``operators``
    the six refuse every request, which is the fail-closed default rather than an oversight.

    The counter is passed in rather than handed back because falcon's ``App`` uses ``__slots__``
    and will not carry an attribute for us; the caller that wants the numbers is the same caller
    that wires up metric export, so it can simply own the object.

    Every request writes one JSON line to stderr (@79mupg4t); ``access_log`` replaces that
    middleware, which is how tests capture the lines.
    """
    app = falcon.App(middleware=[
        access_log or AccessLog(), RequestIdMiddleware(), counter or RequestCounter(),
    ])
    # falcon serializes resp.media only for media types it has a handler for, so setting the
    # RFC 9457 content type without registering it turns every error into a 415 — the error
    # about the error, which is the least useful response there is.
    app.resp_options.media_handlers[PROBLEM_JSON] = falcon.media.JSONHandler()
    guard = OperatorGuard(operators, max_age=max_age, authorities=authorities, clock=clock)
    app.add_route("/v1/witness/health", Endpoint(reader.health))
    app.add_route("/v1/witness/identity", Endpoint(reader.identity))
    app.add_route("/v1/witness/version", Endpoint(reader.version))
    app.add_route("/v1/witness/tags", Endpoint(reader.tags))
    app.add_route("/v1/witness/attribs", Endpoint(reader.attribs))
    app.add_route("/v1/witness/loop", Endpoint(reader.loop, guard))
    app.add_route("/v1/witness/escrow", Endpoint(reader.escrow, guard))
    app.add_route("/v1/witness/database", Endpoint(reader.database, guard))
    app.add_route("/v1/witness/process", Endpoint(reader.process, guard))
    app.add_route("/v1/witness/controller", Endpoint(reader.controllers, guard))
    app.add_route("/v1/witness/controller/{aid}", Endpoint(reader.controller, guard))
    return app
