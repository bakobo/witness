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


# -- the hostile pass on #31 ----------------------------------------------------------------------

def _serve_once(raw):
    """A one-shot HTTP server on loopback that writes ``raw`` and closes; returns its URL."""
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(raw)
        conn.close()
        listener.close()

    threading.Thread(target=answer, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}/v1/witness/controller"


def _real(argv):
    out, err = io.StringIO(), io.StringIO()
    _sub, cfg = config.parse_args(["get", *argv])
    return get.run(cfg, out=out, err=err), out.getvalue(), err.getvalue()


def test_a_redirect_is_not_followed_so_the_signature_stays_home(key_file):
    """Finding 1. urllib re-sends the original headers, Signature included, to the Location."""
    path, _key = key_file
    url = _serve_once(b"HTTP/1.1 302 Found\r\nLocation: http://attacker.invalid/collect\r\n"
                      b"Content-Length: 0\r\nConnection: close\r\n\r\n")

    code, _out, err = _real(["--key", str(path), url])

    assert code == 1
    assert "302" in err


def test_a_truncated_answer_is_not_success(key_file):
    """Finding 2. A JSON prefix and an early close must not print as the answer."""
    path, _key = key_file
    url = _serve_once(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 100\r\nConnection: close\r\n\r\n{}")

    code, out, err = _real(["--key", str(path), url])

    assert (code, out) == (1, "")
    assert err.startswith("e.env.control.incomplete.r: ")


@pytest.mark.parametrize("length", [b"100 ", b"1" * 5000, b"-1", b"abc"])
def test_a_content_length_that_is_padded_long_or_not_a_number_is_not_success(key_file, length):
    """The fix pass on #31: a padded length skipped the check, and a 5000-digit one overflowed int()."""
    path, _key = key_file
    url = _serve_once(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                      b"Content-Length: " + length + b"\r\nConnection: close\r\n\r\n{}")

    code, out, err = _real(["--key", str(path), url])

    assert (code, out) == (1, "")
    assert err.startswith("e.env.control.incomplete.r: ")


def test_broken_chunk_framing_is_a_coded_refusal_not_a_traceback(key_file):
    """Finding 3."""
    path, _key = key_file
    url = _serve_once(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                      b"zz\r\nnot-a-chunk\r\n")

    code, out, err = _real(["--key", str(path), url])

    assert (code, out) == (1, "")
    assert err.startswith("e.env.control.incomplete.r: ")


def test_a_broken_refusal_body_is_also_a_coded_refusal(key_file):
    path, _key = key_file
    url = _serve_once(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 50\r\nConnection: close\r\n\r\n{")

    code, _out, err = _real(["--key", str(path), url])

    assert code == 1
    assert err.startswith("e.env.control.incomplete.r: ")


def test_a_reply_that_is_not_http_is_incomplete_rather_than_unreachable(key_file):
    """The ds seat on the fix diff: BadStatusLine means the peer was reached and spoke nonsense."""
    path, _key = key_file
    url = _serve_once(b"SSH-2.0-OpenSSH_9.6\r\n")

    code, _out, err = _real(["--key", str(path), url])

    assert code == 1
    assert err.startswith("e.env.control.incomplete.r: ")


@pytest.mark.parametrize("url", ["http://example.com/x#fragment", "http://example.com/x?y=é"])
def test_a_url_fiki_will_not_sign_is_an_argument_error_not_a_traceback(key_file, url):
    """Finding 4."""
    path, _key = key_file

    code, _out, err = _run(["--key", str(path), url], lambda *a, **k: pytest.fail("fetched"))

    assert code == 1
    assert err.startswith("e.input.format.f: ")
