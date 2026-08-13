"""falcon app — /healthz and /info happy paths plus their 503 unavailable bodies, driven by
falcon's TestClient against stub readers so no real DB is needed."""

import falcon
import falcon.testing
import pytest

from witness.app import make_app
from witness.errors import DbUnavailable, IdentityUnavailable


class _OkReader:
    def health(self):
        return {"status": "ok"}

    def info(self):
        return {
            "aid": "BExampleAidPrefix",
            "alias": "wit",
            "keripy_version": "2.0.0-dev6",
            "db_path": "/tmp/keri/db/testwit",
        }


class _DownReader:
    def health(self):
        raise DbUnavailable("The witness database could not be opened for reading.")

    def info(self):
        raise DbUnavailable("The witness database could not be opened for reading.")


class _NoIdentityReader:
    def health(self):
        return {"status": "ok"}

    def info(self):
        raise IdentityUnavailable("The witness database opened but holds no witness identity.")


@pytest.fixture
def ok_client():
    return falcon.testing.TestClient(make_app(_OkReader()))


def test_healthz_returns_200_ok(ok_client):
    resp = ok_client.simulate_get("/healthz")
    assert resp.status == falcon.HTTP_200
    assert resp.json == {"status": "ok"}


def test_info_returns_200_with_the_identity(ok_client):
    resp = ok_client.simulate_get("/info")
    assert resp.status == falcon.HTTP_200
    assert resp.json == {
        "aid": "BExampleAidPrefix",
        "alias": "wit",
        "keripy_version": "2.0.0-dev6",
        "db_path": "/tmp/keri/db/testwit",
    }


def test_healthz_returns_503_unavailable_body_when_db_is_down():
    client = falcon.testing.TestClient(make_app(_DownReader()))
    resp = client.simulate_get("/healthz")
    assert resp.status == falcon.HTTP_503
    assert resp.json == {
        "status": "unavailable",
        "error": {
            "code": "witness.db.unavailable",
            "message": "The witness database could not be opened for reading.",
            "retryable": True,
        },
    }


def test_info_returns_503_error_body_when_db_is_down():
    client = falcon.testing.TestClient(make_app(_DownReader()))
    resp = client.simulate_get("/info")
    assert resp.status == falcon.HTTP_503
    assert resp.json == {
        "code": "witness.db.unavailable",
        "message": "The witness database could not be opened for reading.",
        "retryable": True,
    }


def test_info_returns_503_error_body_when_identity_is_missing():
    client = falcon.testing.TestClient(make_app(_NoIdentityReader()))
    resp = client.simulate_get("/info")
    assert resp.status == falcon.HTTP_503
    assert resp.json == {
        "code": "witness.identity.unavailable",
        "message": "The witness database opened but holds no witness identity.",
        "retryable": True,
    }
