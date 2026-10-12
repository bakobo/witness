"""The control plane's structured request log (@79mupg4t).

One JSON line per request, carrying exactly the documented fields. The assertions that matter most
are the negative ones: a refused request names no signer it did not verify, nothing that would let
a log reader replay a request reaches the line, and nothing a caller sends can make the line long
or break it across lines. The rest prove that logging can neither fail a request nor slow one.
"""

import io
import json
import re

import pytest
from falcon import testing
from fiki import Key, sign_request

from test_app import StubReader
from witness import accesslog
from witness.accesslog import FIELDS, MAX_LINE, AccessLog, write_line
from witness.app import make_app
from witness.errors import ControlDenied, ControlUnauthenticated, DbUnavailable

HOST = "falconframework.org"
NOW = 1_800_000_000
SIGNED_ROUTE = "/v1/witness/controller/BSomeAid"


class Captured:
    """An emitter that keeps every line, standing in for the writer thread."""

    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    def only(self):
        assert len(self.lines) == 1, self.lines
        return json.loads(self.lines[0])


@pytest.fixture
def operator():
    return Key.generate()


@pytest.fixture
def captured():
    return Captured()


def _client(captured, operators=(), reader=None):
    app = make_app(reader or StubReader(), operators=operators, clock=lambda: NOW,
                   access_log=AccessLog(captured))
    return testing.TestClient(app)


def _signed(key, route=SIGNED_ROUTE, *, created=NOW):
    return sign_request(key=key, method="GET", url=f"http://{HOST}{route}", created=created)


def _keyid(key):
    """The keyid fiki puts in Signature-Input for ``key``, read back from a real signature."""
    return re.search(r'keyid="([^"]+)"', _signed(key)["Signature-Input"]).group(1)


def test_the_documented_field_set_is_exactly_these_names():
    """The names are the contract a sibling service copies (@79mupg4t), so pin them here."""
    assert FIELDS == (
        "event.name",
        "bakobo.request.id",
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "bakobo.control.outcome",
        "error.type",
        "enduser.id",
    )


def test_an_open_read_logs_one_json_line_with_exactly_the_documented_fields(captured):
    response = _client(captured).simulate_get("/v1/witness/health")

    line = captured.only()
    assert tuple(line) == FIELDS
    assert line == {
        "event.name": "bakobo.control.request",
        "bakobo.request.id": response.headers["Bakobo-Request-Id"],
        "http.request.method": "GET",
        "http.route": "/v1/witness/health",
        "http.response.status_code": 200,
        "bakobo.control.outcome": "open",
        "error.type": None,
        "enduser.id": None,
    }


def test_a_line_is_one_line(captured):
    _client(captured).simulate_get("/v1/witness/health")

    assert "\n" not in captured.lines[0]


def test_an_admitted_read_names_its_verified_signer_and_the_template_not_the_path(
    captured, operator
):
    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE, headers=_signed(operator))

    line = captured.only()
    assert line["bakobo.control.outcome"] == "admitted"
    assert line["enduser.id"] == operator.aid
    assert line["http.route"] == "/v1/witness/controller/{aid}"
    assert line["http.response.status_code"] == 200
    assert line["error.type"] is None


def test_the_request_id_is_the_one_the_caller_supplied_when_it_is_acceptable(captured):
    _client(captured).simulate_get("/v1/witness/health",
                                   headers={"Bakobo-Request-Id": "trace-42"})

    assert captured.only()["bakobo.request.id"] == "trace-42"


def test_an_unsigned_read_is_unauthenticated_and_names_nobody(captured, operator):
    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE)

    line = captured.only()
    assert line["bakobo.control.outcome"] == "unauthenticated"
    assert line["error.type"] == ControlUnauthenticated.code
    assert line["http.response.status_code"] == 401
    assert line["enduser.id"] is None


def test_a_stale_signature_from_a_real_operator_names_nobody(captured, operator):
    """The key is genuine and the signature once was, but fiki refused it, so nothing was proved
    about who sent it today."""
    headers = _signed(operator, created=NOW - 3600)

    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE, headers=headers)

    line = captured.only()
    assert line["bakobo.control.outcome"] == "unauthenticated"
    assert line["enduser.id"] is None
    assert operator.aid not in captured.lines[0]


def test_a_forged_claim_to_be_an_operator_is_not_written_into_the_log(captured, operator):
    """A stranger signs, then relabels the signature as the operator's. The claimed AID is a claim,
    and logging claims would let a forger put any name in the audit trail."""
    stranger = Key.generate()
    headers = {name: value.replace(_keyid(stranger), _keyid(operator))
               for name, value in _signed(stranger).items()}
    assert _keyid(operator) in headers["Signature-Input"]

    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE, headers=headers)

    line = captured.only()
    assert line["bakobo.control.outcome"] == "unauthenticated"
    assert line["enduser.id"] is None
    assert operator.aid not in captured.lines[0]
    assert stranger.aid not in captured.lines[0]


def test_a_genuine_signer_who_is_not_an_operator_is_denied_and_named(captured, operator):
    """The signature verified, so the AID is proved; who is knocking is the point of the record."""
    stranger = Key.generate()

    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE, headers=_signed(stranger))

    line = captured.only()
    assert line["bakobo.control.outcome"] == "denied"
    assert line["error.type"] == ControlDenied.code
    assert line["http.response.status_code"] == 403
    assert line["enduser.id"] == stranger.aid


def test_with_no_operator_configured_the_refusal_names_nobody(captured, operator):
    """The guard refuses before it verifies anything, so there is no signer to name."""
    _client(captured).simulate_get(SIGNED_ROUTE, headers=_signed(operator))

    line = captured.only()
    assert line["bakobo.control.outcome"] == "denied"
    assert line["enduser.id"] is None


def test_no_signature_material_reaches_the_line(captured, operator):
    headers = _signed(operator)

    _client(captured, [operator.aid]).simulate_get(SIGNED_ROUTE, headers=headers)

    text = captured.lines[0]
    for value in headers.values():
        assert value not in text
    assert "sig1" not in text
    assert "BSomeAid" not in text  # the raw path's caller-chosen segment


def test_a_reader_failure_after_admission_stays_admitted_and_carries_its_code(
    captured, operator
):
    reader = StubReader(controller=DbUnavailable("No database."))

    _client(captured, [operator.aid], reader).simulate_get(SIGNED_ROUTE,
                                                           headers=_signed(operator))

    line = captured.only()
    assert line["bakobo.control.outcome"] == "admitted"
    assert line["error.type"] == DbUnavailable.code
    assert line["http.response.status_code"] == 503


def test_an_open_read_that_fails_is_open_with_its_code(captured):
    _client(captured, reader=StubReader(health=DbUnavailable("No database."))).simulate_get(
        "/v1/witness/health"
    )

    line = captured.only()
    assert (line["bakobo.control.outcome"], line["error.type"]) == ("open", DbUnavailable.code)


def test_a_body_that_cannot_be_serialised_is_logged_as_the_500_it_becomes(captured):
    """falcon serialises media after response middleware, so a line written from the status alone
    would report a 200 the client never got. The log serialises first and logs what happens."""
    response = _client(captured, reader=StubReader(health=object())).simulate_get(
        "/v1/witness/health"
    )

    assert response.status_code == 500
    assert captured.only()["http.response.status_code"] == 500


def test_an_unmatched_path_logs_no_route_and_no_outcome(captured):
    _client(captured).simulate_get("/v1/witness/nothing-here")

    line = captured.only()
    assert line["http.route"] is None
    assert line["bakobo.control.outcome"] is None
    assert line["http.response.status_code"] == 404


def test_a_method_falcon_refuses_itself_reaches_no_guard(captured, operator):
    _client(captured, [operator.aid]).simulate_post(SIGNED_ROUTE)

    line = captured.only()
    assert line["http.request.method"] == "POST"
    assert line["bakobo.control.outcome"] is None
    assert line["http.response.status_code"] == 405


def test_an_unknown_method_is_logged_as_other_whatever_its_length(captured):
    """falcon hands req.method through at any length waitress allows, so the line names only the
    verbs semconv knows and folds the rest into _OTHER, its own rule."""
    _client(captured).simulate_request(method="X" * 5000, path="/v1/witness/health")

    line = captured.only()
    assert line["http.request.method"] == "_OTHER"
    assert len(captured.lines[0]) < MAX_LINE


def test_a_hostile_request_id_is_not_what_gets_logged(captured):
    """_acceptable_request_id refuses it, so the line carries the minted id instead."""
    hostile = "a" * 64 + "\nforged"

    _client(captured).simulate_get("/v1/witness/health",
                                   headers={"Bakobo-Request-Id": hostile})

    logged = captured.only()["bakobo.request.id"]
    assert logged != hostile
    assert len(logged) == 32
    assert "\n" not in captured.lines[0]


def test_the_worst_case_line_fits_the_bound():
    """Every value is bounded at its source (@79mupg4t): a 64-character request id, the longest
    route, the longest known verb, the longest outcome, the longest code this surface raises, and
    a 44-character AID. Built from those maxima, the line still fits."""
    longest = {
        "event.name": accesslog.EVENT_NAME,
        "bakobo.request.id": "x" * 64,
        "http.request.method": max(accesslog.KNOWN_METHODS | {"_OTHER"}, key=len),
        "http.route": "/v1/witness/controller/{aid}",
        "http.response.status_code": 599,
        "bakobo.control.outcome": max(accesslog.OUTCOMES, key=len),
        "error.type": max((DbUnavailable.code, ControlDenied.code,
                           ControlUnauthenticated.code, "e.env.telemetry.unavailable.r",
                           "e.self.config.telemetry.f"), key=len),
        "enduser.id": "B" * 44,
    }

    assert len(accesslog.render(longest)) < MAX_LINE


def test_a_line_that_cannot_be_emitted_does_not_fail_the_request():
    def broken(line):
        raise OSError("the journal is gone")

    app = make_app(StubReader(), access_log=AccessLog(broken))

    assert testing.TestClient(app).simulate_get("/v1/witness/health").status_code == 200


def test_write_line_writes_one_flushed_line_to_stderr_as_it_is_at_write_time(monkeypatch):
    """Resolved late, so a redirected stderr (or pytest's capture) is honoured."""
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    write_line('{"a":1}')
    write_line('{"b":2}')

    assert stream.getvalue() == '{"a":1}\n{"b":2}\n'


def test_make_app_logs_to_stderr_by_default(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    testing.TestClient(make_app(StubReader())).simulate_get("/v1/witness/health")

    assert json.loads(stream.getvalue().splitlines()[-1])["http.route"] == "/v1/witness/health"


def test_a_broken_stderr_does_not_fail_the_request(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise OSError("broken pipe")

    monkeypatch.setattr("sys.stderr", Broken())

    response = testing.TestClient(make_app(StubReader())).simulate_get("/v1/witness/health")

    assert response.status_code == 200
