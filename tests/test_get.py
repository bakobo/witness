"""``witness get``: one signed read of a control plane, for an operator (@34sgjmxr).

The signed nouns are useless to a person with curl, so the CLI that serves them also asks for
them. The key is an unencrypted OpenSSH Ed25519 private key, as ``ssh-keygen -t ed25519 -N ''``
writes it, which is the one private-key format fiki reads.
"""

import io
import json
import urllib.error

import fiki
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from witness import config, get
from witness.errors import InvalidArguments

URL = "http://127.0.0.1:5633/v1/witness/controller"


@pytest.fixture
def key_file(tmp_path):
    private = Ed25519PrivateKey.generate()
    text = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                 serialization.NoEncryption())
    path = tmp_path / "operator"
    path.write_bytes(text)
    return path, fiki.Key.from_seed(private.private_bytes_raw())


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(argv, opener):
    out, err = io.StringIO(), io.StringIO()
    _sub, cfg = config.parse_args(["get", *argv])
    return get.run(cfg, urlopen=opener, out=out, err=err), out.getvalue(), err.getvalue()


def test_it_signs_the_read_and_prints_the_answer(key_file):
    path, key = key_file
    seen = {}

    def opener(request, timeout):
        seen.update(request=request, timeout=timeout)
        return Response(b'{"controllers": []}')

    code, out, _err = _run(["--key", str(path), URL], opener)

    assert (code, json.loads(out)) == (0, {"controllers": []})
    headers = dict(seen["request"].header_items())
    verdict = fiki.verify_request(method="GET", url=URL, headers=headers, max_age=60,
                                  authorities=None)
    assert verdict.aid == key.aid
    assert seen["timeout"] > 0


def test_it_prints_the_keys_aid_for_naming_as_an_operator(key_file):
    path, key = key_file

    code, out, _err = _run(["--key", str(path), "--aid"], lambda *a, **k: pytest.fail("fetched"))

    assert (code, out) == (0, f"{key.aid}\n")


def test_a_refusal_prints_the_problem_and_its_code_and_exits_one(key_file):
    path, _key = key_file
    problem = b'{"code": "e.grant.missing.operator.f", "detail": "Not an operator."}'

    def opener(request, timeout):
        raise urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(problem))

    code, out, err = _run(["--key", str(path), URL], opener)

    assert code == 1
    assert json.loads(out)["code"] == "e.grant.missing.operator.f"
    assert err.startswith("e.grant.missing.operator.f: ")


def test_a_refusal_without_a_problem_body_still_says_what_happened(key_file):
    path, _key = key_file

    def opener(request, timeout):
        raise urllib.error.HTTPError(URL, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    code, _out, err = _run(["--key", str(path), URL], opener)

    assert code == 1
    assert "502" in err


def test_an_unreachable_control_plane_is_a_retryable_error(key_file):
    path, _key = key_file

    def opener(request, timeout):
        raise urllib.error.URLError("connection refused")

    code, out, err = _run(["--key", str(path), URL], opener)

    assert (code, out) == (1, "")
    assert err.startswith("e.env.control.unavailable.r: ")


def test_an_oversized_answer_is_refused_rather_than_read_whole(key_file, monkeypatch):
    path, _key = key_file
    monkeypatch.setattr(get, "MAX_RESPONSE_BYTES", 8)

    code, out, err = _run(["--key", str(path), URL], lambda request, timeout: Response(b"x" * 9))

    assert (code, out) == (1, "")
    assert err.startswith("e.input.range.response.f: ")


def test_a_missing_key_file_is_refused(tmp_path):
    code, _out, err = _run(["--key", str(tmp_path / "absent"), URL], None)

    assert code == 1
    assert err.startswith("e.input.format.operatorkey.f: ")


def test_a_key_fiki_cannot_read_is_refused_without_quoting_it(tmp_path):
    path = tmp_path / "operator"
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecretish\n")

    code, _out, err = _run(["--key", str(path), URL], None)

    assert code == 1
    assert err.startswith("e.input.format.operatorkey.f: ")
    assert "secretish" not in err


def test_an_oversized_key_file_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "operator"
    path.write_text("x" * (get.MAX_KEY_BYTES + 1))

    code, _out, err = _run(["--key", str(path), URL], None)

    assert (code, err.startswith("e.input.format.operatorkey.f: ")) == (1, True)


@pytest.mark.parametrize("url", ["ftp://h/x", "not a url", "http:///nohost", "http://h:99999/"])
def test_only_an_http_url_is_fetched(url):
    with pytest.raises(InvalidArguments, match="http"):
        config.parse_args(["get", "--key", "k", url])


def test_a_url_is_needed_unless_only_the_aid_is_wanted():
    with pytest.raises(InvalidArguments, match="URL"):
        config.parse_args(["get", "--key", "k"])
    _sub, cfg = config.parse_args(["get", "--key", "k", "--aid"])
    assert (cfg.print_aid, cfg.url) == (True, None)


def test_the_url_is_bounded():
    with pytest.raises(InvalidArguments, match="at most"):
        config.parse_args(["get", "--key", "k", "http://h/" + "x" * 3000])
