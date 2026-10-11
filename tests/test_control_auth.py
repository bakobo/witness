"""Which control-plane reads are signed, and what a refusal looks like (@34sgjmxr, @s6v3qm).

The split is the contract: five nouns answer anyone, six answer only an operator who signs the
request with RFC 9421 via fiki. A witness started with no operator still serves the five, so the
image's healthcheck and the deployment's probes keep working, and refuses the six outright.
"""

import pytest
from falcon import testing
from fiki import Key, sign_request

from test_app import StubReader
from witness.app import make_app

HOST = "falconframework.org"  # what falcon's test client puts in the Host header
OPEN = [
    "/v1/witness/health",
    "/v1/witness/identity",
    "/v1/witness/version",
    "/v1/witness/tags",
    "/v1/witness/attribs",
]
SIGNED = [
    "/v1/witness/loop",
    "/v1/witness/escrow",
    "/v1/witness/database",
    "/v1/witness/process",
    "/v1/witness/controller",
    "/v1/witness/controller/BSomeAid",
]
NOW = 1_800_000_000


@pytest.fixture
def operator():
    return Key.generate()


def _app(operators=(), **kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    return testing.TestClient(make_app(StubReader(), operators=operators, **kwargs))


def _signed(key, route, *, created=NOW, host=HOST, **extra):
    return sign_request(key=key, method="GET", url=f"http://{host}{route}", created=created,
                        **extra)


@pytest.mark.parametrize("route", OPEN)
def test_the_open_nouns_answer_without_a_signature(route):
    assert _app().simulate_get(route).status_code == 200


@pytest.mark.parametrize("route", SIGNED)
def test_an_unsigned_request_for_a_signed_noun_is_unauthenticated(operator, route):
    response = _app([operator.aid]).simulate_get(route)

    assert (response.status_code, response.json["code"]) == (401, "e.id.invalid.control.f")
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("route", SIGNED)
def test_an_operator_signed_request_is_answered(operator, route):
    response = _app([operator.aid]).simulate_get(route, headers=_signed(operator, route))

    assert response.status_code == 200


def test_a_genuine_signer_who_is_not_an_operator_is_denied(operator):
    stranger = Key.generate()
    route = "/v1/witness/controller"

    response = _app([operator.aid]).simulate_get(route, headers=_signed(stranger, route))

    assert (response.status_code, response.json["code"]) == (403, "e.grant.missing.operator.f")
    assert response.json["args"] == [stranger.aid]


@pytest.mark.parametrize("route", SIGNED)
def test_with_no_operator_configured_the_signed_nouns_refuse_everyone(operator, route):
    """Fail closed. Not even a valid signature gets in when nobody was named, and the detail says
    how to name somebody, because the likeliest reader is an operator who just upgraded."""
    response = _app().simulate_get(route, headers=_signed(operator, route))

    assert (response.status_code, response.json["code"]) == (403, "e.grant.missing.operator.f")
    assert "WITNESS_OPERATORS" in response.json["detail"]


def test_a_signature_for_another_path_does_not_open_this_one(operator):
    headers = _signed(operator, "/v1/witness/health")

    response = _app([operator.aid]).simulate_get("/v1/witness/controller", headers=headers)

    assert response.status_code == 401


def test_a_stale_signature_is_refused(operator):
    route = "/v1/witness/escrow"
    headers = _signed(operator, route, created=NOW - 3600)

    response = _app([operator.aid], max_age=60).simulate_get(route, headers=headers)

    assert response.status_code == 401


def test_max_age_is_the_configured_window(operator):
    route = "/v1/witness/escrow"
    headers = _signed(operator, route, created=NOW - 100)

    assert _app([operator.aid], max_age=60).simulate_get(route, headers=headers).status_code == 401
    assert _app([operator.aid], max_age=300).simulate_get(route, headers=headers).status_code == 200


def test_a_get_carrying_a_body_is_refused(operator):
    """A read has no body. fiki refuses one its signature does not bind, so nothing unsigned can
    ride in alongside an operator's request."""
    route = "/v1/witness/loop"
    headers = {**_signed(operator, route), "Content-Length": "2"}

    response = _app([operator.aid]).simulate_get(route, headers=headers, body="{}")

    assert response.status_code == 401


def test_without_authorities_any_dialled_address_is_accepted(operator):
    """The authority is whatever address the caller dialled, which a tunnel or a pool port makes
    unknowable in advance; @34sgjmxr leaves the check opt-in."""
    route = "/v1/witness/loop"
    response = _app([operator.aid]).simulate_get(route, headers=_signed(operator, route))

    assert response.status_code == 200


def test_configured_authorities_refuse_a_request_signed_for_another_witness(operator):
    route = "/v1/witness/loop"
    elsewhere = _signed(operator, route)
    client = _app([operator.aid], authorities=("witness-b.example:5633",))

    assert client.simulate_get(route, headers=elsewhere).status_code == 401


def test_configured_authorities_admit_a_request_signed_for_this_witness(operator):
    route = "/v1/witness/loop"
    here = _signed(operator, route)

    assert _app([operator.aid], authorities=(HOST,)).simulate_get(route, headers=here).status_code \
        == 200


def test_a_refusal_happens_before_the_reader_is_asked_anything(operator):
    """The reader opens LMDB; a stranger must not be able to make it do so."""
    asked = []

    class Watching(StubReader):
        def controllers(self):
            asked.append("controllers")
            return super().controllers()

    client = testing.TestClient(make_app(Watching(), operators=(operator.aid,)))
    client.simulate_get("/v1/witness/controller")

    assert asked == []
