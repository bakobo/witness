"""The Registrar's HTTP surface: authenticated publishers, subscriptions, and nothing to pull."""

import base64
import json

import falcon.testing
import fiki
import pytest

from witness.registrar.app import make_registrar_app
from witness.registrar.store import RegistrarStore

BASE = "http://falconframework.org"
PUBLICATION = "/v1/registrar/publication"
SUBSCRIPTION = "/v1/registrar/subscription"


@pytest.fixture
def publisher():
    return fiki.Key.generate()


@pytest.fixture
def client(tmp_path, publisher):
    store = RegistrarStore(tmp_path / "registrar.sqlite3")
    app = make_registrar_app(store, publishers=frozenset({publisher.aid}), max_age=60)
    yield falcon.testing.TestClient(app), store
    store.close()


def _signed(client, key, method, path, document=None, *, raw=None, sign=True):
    body = raw if raw is not None else (None if document is None
                                        else json.dumps(document).encode())
    headers = (fiki.sign_request(key=key, method=method, url=BASE + path, body=body)
               if sign else {})
    return client.simulate_request(method, path, headers=headers, body=body)


def _snapshot(data=b"rip+iss", **changes):
    return {"registry": "EReg" + "r" * 40, "issuer": "EIss" + "i" * 40,
            "digest": "d" * 64, "chain": base64.b64encode(data).decode(),
            "kel": base64.b64encode(b"kel").decode(), **changes}


def test_a_configured_publisher_publishes_and_a_repeat_is_reacknowledged(client, publisher):
    app, store = client
    answer = _signed(app, publisher, "POST", PUBLICATION, _snapshot())
    assert (answer.status_code, answer.json) == (200, {"digest": "d" * 64})
    assert store.head("EReg" + "r" * 40)["chain"] == b"rip+iss"
    assert _signed(app, publisher, "POST", PUBLICATION, _snapshot()).status_code == 200


def test_a_stale_or_forked_publication_is_a_named_conflict(client, publisher):
    app, _ = client
    _signed(app, publisher, "POST", PUBLICATION, _snapshot(b"rip+iss+rev"))
    stale = _signed(app, publisher, "POST", PUBLICATION, _snapshot(b"rip+iss"))
    assert (stale.status_code, stale.json["code"]) == (409, "e.state.conflict.registrar.stale.f")
    fork = _signed(app, publisher, "POST", PUBLICATION, _snapshot(b"rip+other"))
    assert (fork.status_code, fork.json["code"]) == (409, "e.state.conflict.registrar.fork.f")


def test_an_unsigned_or_unknown_publisher_is_refused(client, publisher):
    app, store = client
    unsigned = _signed(app, publisher, "POST", PUBLICATION, _snapshot(), sign=False)
    assert (unsigned.status_code, unsigned.json["code"]) == \
        (401, "e.auth.signature.registrar.f")
    stranger = _signed(app, fiki.Key.generate(), "POST", PUBLICATION, _snapshot())
    assert (stranger.status_code, stranger.json["code"]) == (403, "e.grant.denied.registrar.f")
    assert store.head("EReg" + "r" * 40) is None


def test_a_body_that_does_not_match_its_signature_is_refused(client, publisher):
    app, _ = client
    body = json.dumps(_snapshot()).encode()
    headers = fiki.sign_request(key=publisher, method="POST", url=BASE + PUBLICATION,
                                body=body)
    tampered = app.simulate_request("POST", PUBLICATION, headers=headers,
                                    body=body.replace(b"ddd", b"eee", 1))
    assert tampered.json["code"] == "e.auth.signature.registrar.f"


def test_a_signature_that_does_not_cover_the_body_is_refused(client, publisher):
    app, _ = client
    body = json.dumps(_snapshot()).encode()
    headers = fiki.sign_request(key=publisher, method="POST", url=BASE + PUBLICATION)
    uncovered = app.simulate_request("POST", PUBLICATION, headers=headers, body=body)
    assert (uncovered.status_code, uncovered.json["code"]) == \
        (401, "e.auth.signature.registrar.f")


@pytest.mark.parametrize("bad", [
    {"registry": 7}, {"issuer": ""}, {"digest": "x" * 200}, {"chain": "not base64!"},
    {"kel": None}, {"surprise": 1},
])
def test_a_malformed_publication_is_refused_before_it_is_stored(client, publisher, bad):
    app, store = client
    answer = _signed(app, publisher, "POST", PUBLICATION, _snapshot(**bad))
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")
    assert store.head("EReg" + "r" * 40) is None


@pytest.mark.parametrize("raw", [b"not json", b"[1]", b""])
def test_a_body_that_is_not_a_json_object_is_refused(client, publisher, raw):
    app, _ = client
    answer = _signed(app, publisher, "POST", PUBLICATION, raw=raw)
    assert answer.json["code"] in {"e.input.format.registrar.f",
                                   "e.auth.signature.registrar.f"}
    assert answer.status_code in {400, 401}


def test_an_oversized_body_is_refused_unread(client, publisher, monkeypatch):
    from witness.registrar import app as registrar_app
    monkeypatch.setattr(registrar_app, "MAX_BODY", 10)
    app, _ = client
    answer = _signed(app, publisher, "POST", PUBLICATION, _snapshot())
    assert (answer.status_code, answer.json["code"]) == (413, "e.input.range.registrar.f")


def test_any_signer_can_subscribe_once_and_unsubscribe(client):
    app, store = client
    observer = fiki.Key.generate()
    answer = _signed(app, observer, "POST", SUBSCRIPTION,
                     {"callback": "http://127.0.0.1:9000/batch"})
    assert (answer.status_code, answer.json) == (200, {"subscriber": observer.aid,
                                                       "registrar": store.key().aid})
    assert store.subscribers() == [(observer.aid, "http://127.0.0.1:9000/batch")]
    assert _signed(app, observer, "DELETE", SUBSCRIPTION).status_code == 204
    again = _signed(app, observer, "DELETE", SUBSCRIPTION)
    assert (again.status_code, again.json["code"]) == (404, "e.state.missing.subscription.f")


@pytest.mark.parametrize("callback", ["ftp://127.0.0.1/x", "not a url", 5,
                                      "http://" + "h" * 300 + "/"])
def test_a_subscription_needs_an_http_callback(client, callback):
    app, store = client
    answer = _signed(app, fiki.Key.generate(), "POST", SUBSCRIPTION, {"callback": callback})
    assert (answer.status_code, answer.json["code"]) == (400, "e.input.format.registrar.f")
    assert store.subscribers() == []


def test_there_is_nothing_to_pull(client, publisher):
    """No verification anywhere may cause Registrar traffic, so no read route exists at all."""
    app, _ = client
    _signed(app, publisher, "POST", PUBLICATION, _snapshot())
    for path in ("/v1/registrar/publication", "/v1/registrar/head", "/v1/registrar/tel/EReg"):
        assert app.simulate_get(path).status_code in {404, 405}
