"""The control-plane HTTP surface: routes, envelope, and status mapping (@zzbdxa).

Every assertion here is about the *contract* rather than the data — that the paths follow
``/v1/witness/<noun>``, that a typed error renders as one RFC 9457 problem document whatever
endpoint raised it, and that the status comes from the error's code prefix. The sibling
inconsistency @zzbdxa found (``/healthz`` nesting its error under "error" while ``/info`` did
not) is exactly what a shared envelope test prevents recurring.
"""

import pytest
from falcon import testing

from witness.app import make_app
from witness.errors import ControllerUnknown, DbUnavailable, WitnessNotIncepted


class StubReader:
    """A reader whose every view can be made to succeed or raise."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def _answer(self, name, default):
        value = self._overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def health(self):
        return self._answer("health", {"status": "ok", "ticks": 12})

    def identity(self):
        return self._answer("identity", {"aid": "BAid", "alias": "witness"})

    def version(self):
        return self._answer("version", {"witness": "0.1.0", "keripy": "2.0.0-dev6"})

    def loop(self):
        return self._answer("loop", {"ticks": 12, "loop_lag": 0.0, "doers": []})

    def escrow(self):
        return self._answer("escrow", {"depths": {"query_not_found": 0}, "total": 0})

    def database(self):
        return self._answer("database", {"used_bytes": 1, "map_bytes": 2})

    def process(self):
        return self._answer("process", {"pid": 9})

    def controllers(self):
        return self._answer("controllers", {"controllers": []})

    def controller(self, aid):
        value = self._overrides.get("controller", {"aid": aid})
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def client():
    return testing.TestClient(make_app(StubReader()))


ROUTES = [
    "/v1/witness/health",
    "/v1/witness/identity",
    "/v1/witness/version",
    "/v1/witness/loop",
    "/v1/witness/escrow",
    "/v1/witness/database",
    "/v1/witness/process",
    "/v1/witness/controller",
    "/v1/witness/controller/BSomeAid",
]


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_answers_under_the_versioned_component_prefix(client, route):
    assert client.simulate_get(route).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_starts_with_the_two_fixed_segments(route):
    """url-design.md: the version governs the meaning of everything after it, and the component
    is the boundary along which services are split and versioned."""
    assert route.split("/")[1:3] == ["v1", "witness"]


def test_the_old_unversioned_paths_are_gone(client):
    """@zzbdxa re-cut rather than grandfathered, so the pre-standard paths must not linger."""
    assert client.simulate_get("/healthz").status_code == 404
    assert client.simulate_get("/info").status_code == 404


def test_a_typed_error_renders_as_one_rfc9457_problem_document():
    client = testing.TestClient(make_app(StubReader(identity=DbUnavailable("No database."))))

    response = client.simulate_get("/v1/witness/identity")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json == {
        "code": "e.env.witnessdb.unavailable.r",
        "type": "https://errors.bakobo.com/e.env.witnessdb.unavailable.r",
        "title": "I could not open the witness database.",
        "detail": "No database.",
        "instance": "/v1/witness/identity",
        "request_id": response.headers["Bakobo-Request-Id"],
    }


@pytest.mark.parametrize(
    ("view", "error", "status"),
    [
        ("health", DbUnavailable("x."), 503),
        ("identity", WitnessNotIncepted("x."), 409),
        ("controller", ControllerUnknown("x."), 404),
    ],
)
def test_the_status_comes_from_the_errors_code_not_the_call_site(view, error, status):
    client = testing.TestClient(make_app(StubReader(**{view: error})))
    route = "/v1/witness/controller/BAid" if view == "controller" else f"/v1/witness/{view}"

    assert client.simulate_get(route).status_code == status


def test_a_retryable_error_says_when_to_come_back():
    """http-errors.md requires Retry-After on e.state.pending, and the same promise is honest
    wherever the disposition says a retry could help."""
    client = testing.TestClient(make_app(StubReader(health=WitnessNotIncepted("Not yet."))))

    response = client.simulate_get("/v1/witness/health")

    assert response.headers["retry-after"] == "5"


def test_a_permanent_error_does_not_invite_a_retry():
    client = testing.TestClient(make_app(StubReader(controller=ControllerUnknown("Nope."))))

    response = client.simulate_get("/v1/witness/controller/BAid")

    assert "retry-after" not in {key.lower() for key in response.headers}


def test_a_request_id_is_generated_when_the_caller_supplies_none(client):
    response = client.simulate_get("/v1/witness/health")

    assert response.headers["Bakobo-Request-Id"]


def test_a_callers_request_id_is_carried_through_for_correlation(client):
    response = client.simulate_get(
        "/v1/witness/health", headers={"Bakobo-Request-Id": "01CALLERGAVETHIS"}
    )

    assert response.headers["Bakobo-Request-Id"] == "01CALLERGAVETHIS"


def test_a_path_parameter_reaches_the_view(client):
    assert client.simulate_get("/v1/witness/controller/BParticular").json["aid"] == "BParticular"


def test_an_unknown_path_is_a_plain_404(client):
    assert client.simulate_get("/v1/witness/nonsense").status_code == 404


def test_a_wrong_verb_is_refused(client):
    """falcon answers 405 with an Allow header for a known path and an unimplemented method,
    which is what http-errors.md asks for."""
    response = client.simulate_post("/v1/witness/health")

    assert response.status_code == 405
    assert "allow" in {key.lower() for key in response.headers}
