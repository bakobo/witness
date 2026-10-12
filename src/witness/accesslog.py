"""One structured JSON line per control-plane request (@79mupg4t).

The field names are OpenTelemetry semantic conventions where one fits and ``bakobo.``-namespaced
where none does, chosen so a sibling service can copy them verbatim. Every key is on every line,
null where it does not apply, so a consumer reads one shape.

Nothing a caller sends can make a line long or split it, because every value is bounded where it
comes from: the request id by ``_acceptable_request_id``, the route by falcon's finite template
set, the method by :data:`KNOWN_METHODS`, the signer by fiki's 44-character AID, the code by
module literals. And logging cannot fail a request: whatever goes wrong building or writing the
line is swallowed.
"""

from __future__ import annotations

import json
import sys

import falcon

EVENT_NAME = "bakobo.control.request"

#: The documented field set, in the order a line carries it.
FIELDS = (
    "event.name",
    "bakobo.request.id",
    "http.request.method",
    "http.route",
    "http.response.status_code",
    "bakobo.control.outcome",
    "error.type",
    "enduser.id",
)

#: The guard's verdicts. Null, not one of these, when no endpoint ran.
OUTCOMES = frozenset({"open", "admitted", "unauthenticated", "denied"})

#: semconv's known methods. Anything else is logged as ``_OTHER``, which is semconv's own rule and
#: is what keeps a 5000-character method out of the line: falcon passes one through.
KNOWN_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)

#: A line is shorter than this whatever the caller sends; a test holds the worst case under it.
MAX_LINE = 512


def render(record):
    """Serialise one record compactly, as a single line."""
    return json.dumps(record, separators=(",", ":"))


def record_of(req, resp, status=None):
    """The line's fields for one finished request, from what the endpoint left on the context.

    ``status`` overrides the response's own, for a response falcon is about to turn into a 500.
    """
    context = req.context
    return {
        "event.name": EVENT_NAME,
        "bakobo.request.id": getattr(context, "request_id", None),
        "http.request.method": req.method if req.method in KNOWN_METHODS else "_OTHER",
        "http.route": req.uri_template,
        "http.response.status_code": status or int(str(resp.status)[:3]),
        "bakobo.control.outcome": getattr(context, "outcome", None),
        "error.type": getattr(context, "error_type", None),
        "enduser.id": getattr(context, "signer", None),
    }


def write_line(line):  # ~3h2y
    """Write one line to stderr as it is now, and flush, so journald sees it immediately."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


class AccessLog:
    """falcon middleware that hands one rendered line per request to ``emit``."""

    def __init__(self, emit=write_line):
        self._emit = emit

    def process_response(self, req, resp, resource, req_succeeded):
        # falcon serialises media only after every response middleware has run, so a body that
        # cannot be serialised turns a 200 into an error after a line read from resp.status would
        # already say 200. Serialising here, first, makes the logged status the one that is sent;
        # the result is cached on resp, so falcon does not serialise twice. The failure is
        # re-raised so falcon answers it exactly as it would have: with the error's own status
        # when it is an HTTP error (an unrenderable media type is a 415), else a 500. make_app
        # runs this middleware's response step first, so the request counter after it sees the
        # status falcon settles on too.
        try:
            resp.render_body()
        except Exception as failure:
            status = failure.status_code if isinstance(failure, falcon.HTTPError) else 500
            self._write(req, resp, status=status)
            raise
        self._write(req, resp)

    def _write(self, req, resp, status=None):
        try:
            self._emit(render(record_of(req, resp, status)))
        except Exception:  # noqa: BLE001 - a log line is never worth a failed request
            pass
