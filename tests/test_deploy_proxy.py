"""The kit's Caddyfile run by a real Caddy, in front of the real control plane (@9y806dye).

tests/test_deploy_kit.py reads the Caddyfile as text, which can say what a directive is but not
what Caddy does with it: which of two reverse_proxy directives a request reaches, whether Host is
passed through, whether a path is cleaned or a query re-encoded on the way. A watcher's RFC 9421
signature covers @authority, @path and @query, so each of those is a question the signature
answers only if the bytes arrive as signed. This file asks Caddy itself.

The Caddyfile is the kit's own, with four substitutions a test cannot avoid: plain HTTP on a free
port instead of ACME on 443, the admin API off instead of a socket under /run, and both upstreams
on free ports. Each substitution must happen exactly once, so a kit that changed shape fails here
rather than letting a stale rewrite test something else.

Like the smoke tests, this runs shipped processes and is not counted toward coverage. It needs a
``caddy`` binary: ``CADDY`` names one, else PATH is searched. Without one it skips locally and
fails under CI, which installs a pinned Caddy, because a guard that skips where it is meant to run
is not a guard.
"""

from __future__ import annotations

import contextlib
import gzip
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fiki import Key, Request as SignedRequest, sign_request, verify_response
from keri.app import habbing
from witness.changelog import ChangeLog, change_path

CADDYFILE = Path(__file__).resolve().parent.parent / "deploy" / "Caddyfile"
CADDY = os.environ.get("CADDY") or shutil.which("caddy")
HOST = "witness.example.net"

if CADDY is None:  # pragma: no cover - depends on the machine
    if os.environ.get("CI"):
        raise RuntimeError("CI must install caddy for tests/test_deploy_proxy.py; see ci.yml.")
    pytest.skip("no caddy binary: set CADDY or put caddy on PATH.", allow_module_level=True)


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the kit's Caddyfile names {old!r} {text.count(old)} times"
    return text.replace(old, new)


def local_caddyfile(edge: int, witness: int, control: int) -> str:
    text = CADDYFILE.read_text(encoding="utf-8")
    text = _replace_once(text, f"\n{HOST} {{", f"\nhttp://{HOST}:{edge} {{")
    text = _replace_once(text, "admin unix//run/caddy/admin.sock", "admin off")
    text = _replace_once(text, "reverse_proxy 127.0.0.1:5631", f"reverse_proxy 127.0.0.1:{witness}")
    return _replace_once(text, "127.0.0.1:5633", f"127.0.0.1:{control}")


def _wait_for(port: int, proc: subprocess.Popen, what: str) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"{what} exited early ({proc.returncode}): "
                                 f"{proc.stdout.read().decode('utf-8', 'replace')}")
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
            return
        time.sleep(0.2)
    raise AssertionError(f"{what} never listened on {port}")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@contextlib.contextmanager
def caddy(tmp_path: Path, witness: int, control: int):
    edge = _free_port()
    config = tmp_path / "Caddyfile"
    config.write_text(local_caddyfile(edge, witness, control), encoding="utf-8")
    proc = subprocess.Popen([CADDY, "run", "--config", str(config), "--adapter", "caddyfile"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path),
                                 "XDG_CONFIG_HOME": str(tmp_path)})
    try:
        _wait_for(edge, proc, "caddy")
        yield edge
    finally:
        _stop(proc)


class _Stub(http.server.BaseHTTPRequestHandler):
    """An upstream that says which port answered and serves ``body`` for every GET."""

    body = b"keripy witness"

    def do_GET(self):  # noqa: N802 (http.server's spelling)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("X-Stub", "witness")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def stub(body: bytes = _Stub.body):
    handler = type("Stub", (_Stub,), {"body": body})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def fetch(edge: int, method: str, target: str, headers=None, host: str | None = None):
    """One request to Caddy, dialled at loopback but naming ``host`` as a remote client would."""
    conn = http.client.HTTPConnection("127.0.0.1", edge, timeout=60)
    try:
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"{HOST}:{edge}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def deployed(tmp_path_factory):
    """Caddy on the kit's Caddyfile, the real control plane behind it, a stub witness beside it."""
    tmp = tmp_path_factory.mktemp("proxy")
    head = str(tmp / "keristore")
    hby = habbing.Habery(name="proxywit", base="", temp=False, headDirPath=head,
                         bran="abcdefghijk1234567890")
    hby.makeHab(name="wit", transferable=False)
    hby.close()
    ChangeLog.open(change_path(SimpleNamespace(name="proxywit", base="", head_dir_path=head))
                   ).close_clean()

    watcher = Key.generate()
    control = _free_port()
    plane = subprocess.Popen(
        ["uv", "run", "witness", "control-plane", "--name", "proxywit", "--head-dir-path", head,
         "--host", "127.0.0.1", "--port", str(control), "--no-telemetry",
         "--watcher", watcher.aid, "--operator", Key.generate().aid],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(control, plane, "the control plane")
        with stub() as witness, caddy(tmp, witness, control) as edge:
            yield SimpleNamespace(edge=edge, watcher=watcher)
    finally:
        _stop(plane)


def signed_get(deployed, target: str, extra=None):
    """A watcher's GET, signed for the public URL it believes it is calling."""
    url = f"http://{HOST}:{deployed.edge}{target}"
    headers = sign_request(key=deployed.watcher, method="GET", url=url)
    status, resp_headers, body = fetch(deployed.edge, "GET", target, {**headers, **(extra or {})})
    return url, headers, status, resp_headers, body


def change_key(deployed) -> str:
    status, headers, body = fetch(deployed.edge, "GET", "/v1/witness/identity")
    assert status == 200 and headers["Bakobo-Request-Id"]
    return json.loads(body)["change_key"]


def assert_verifies(deployed, url, request_headers, status, resp_headers, body):
    verify_response(status=status, headers=resp_headers, body=body,
                    request=SignedRequest("GET", url, request_headers),
                    expected_aid=change_key(deployed), max_age=60, expected_keyid=None)


def test_a_watcher_signed_request_for_the_public_url_verifies_both_ways(deployed):
    """@authority is rebuilt from Host, which Caddy passes through; @path arrives unprefixed."""
    url, sent, status, headers, body = signed_get(deployed, "/v1/witness/change?limit=1")
    assert status == 200, body
    assert_verifies(deployed, url, sent, status, headers, body)
    assert json.loads(body)["change"]


def test_the_query_reaches_the_control_plane_as_signed(deployed):
    """A cursor round-trips through Caddy byte for byte, percent-encoding included: a re-encoded
    @query would fail the signature, and a dropped one would answer an initial sync instead."""
    _url, _sent, status, _headers, body = signed_get(deployed, "/v1/witness/change?limit=1")
    assert status == 200
    cursor = json.loads(body)["next"]
    target = f"/v1/witness/change?since={quote(cursor, safe='')}&limit=1"
    url, sent, status, headers, body = signed_get(deployed, target)
    assert status == 200, body
    assert_verifies(deployed, url, sent, status, headers, body)

    # The same request with its query re-encoded differently is a different @query, and refused.
    reencoded = f"/v1/witness/change?limit=1&since={quote(cursor, safe='')}"
    sent_for = sign_request(key=deployed.watcher, method="GET",
                            url=f"http://{HOST}:{deployed.edge}{target}")
    status, _headers, _body = fetch(deployed.edge, "GET", reencoded, sent_for)
    assert status == 401


def test_a_compressed_answer_still_verifies(deployed):
    """The kit encodes responses; Content-Digest covers the content, which a client decodes."""
    url, sent, status, headers, body = signed_get(deployed, "/v1/witness/change?limit=1",
                                                  {"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Content-Encoding"] == "gzip"
    assert_verifies(deployed, url, sent, status, headers, gzip.decompress(body))


def test_an_unlisted_signer_is_refused_by_the_feed_itself(deployed):
    url = f"http://{HOST}:{deployed.edge}/v1/witness/change"
    status, headers, _body = fetch(deployed.edge, "GET", "/v1/witness/change",
                                   sign_request(key=Key.generate(), method="GET", url=url))
    assert status == 403 and headers["Bakobo-Request-Id"]


def test_identity_is_open_and_names_the_change_key(deployed):
    assert change_key(deployed).startswith("B")


REFUSED = [
    ("GET", "/v1/witness"), ("GET", "/v1/witness/"), ("GET", "/v1/witness/health"),
    ("GET", "/v1/witness/version"), ("GET", "/v1/witness/tags"), ("GET", "/v1/witness/attribs"),
    ("GET", "/v1/witness/loop"), ("GET", "/v1/witness/escrow"), ("GET", "/v1/witness/database"),
    ("GET", "/v1/witness/process"), ("GET", "/v1/witness/controller"),
    ("GET", "/v1/witness/controller/BAbc"), ("GET", "/v1/witness/change/"),
    ("GET", "/v1/witness/identity/"), ("HEAD", "/v1/witness/change"),
    ("POST", "/v1/witness/change"), ("OPTIONS", "/v1/witness/identity"),
    ("GET", "/V1/WITNESS/LOOP"), ("GET", "/v1/witness/%6coop"), ("GET", "/v1/witness//loop"),
    ("GET", "/v1/witness/change/../loop"), ("GET", "/v1/%77itness/loop"),
    ("GET", "/v1/witness/loop?x=/v1/witness/change"),
]


@pytest.mark.parametrize(("method", "target"), REFUSED, ids=[f"{m} {t}" for m, t in REFUSED])
def test_every_other_control_plane_path_is_refused_at_the_edge(deployed, method, target):
    """404 from Caddy, with no Bakobo-Request-Id: the control plane never saw the request, and the
    stub witness did not answer it either."""
    status, headers, body = fetch(deployed.edge, method, target)
    assert status == 404, (status, body)
    assert "Bakobo-Request-Id" not in headers
    assert "X-Stub" not in headers


def test_the_witness_still_answers_everything_else(deployed):
    status, headers, body = fetch(deployed.edge, "GET", "/oobi")
    assert (status, headers["X-Stub"], body) == (200, "witness", b"keripy witness")


def test_a_request_naming_another_host_reaches_neither_upstream(deployed):
    """Why @authority binding is not configured on the control plane: Caddy proxies only requests
    whose Host names this witness, and Host is what the signature's @authority is rebuilt from, so
    a request signed for a sibling witness cannot be replayed through this one's edge."""
    sibling = f"sibling.example.net:{deployed.edge}"
    url = f"http://{sibling}/v1/witness/change"
    status, headers, body = fetch(deployed.edge, "GET", "/v1/witness/change",
                                  sign_request(key=deployed.watcher, method="GET", url=url),
                                  host=sibling)
    # Caddy's answer when no site matches the Host: an empty 200 from Caddy itself.
    assert (status, body) == (200, b"")
    assert "Bakobo-Request-Id" not in headers and "X-Stub" not in headers


def test_a_full_feed_page_passes_through_whole(tmp_path):
    """A page is at most 16 MiB of replay before JSON and base64 grow it by about a third. Caddy
    must neither cap the answer nor cut it short: 24 MiB, served by a stand-in control plane,
    arrives intact."""
    body = os.urandom(24 * 1024 * 1024)
    with stub() as witness, stub(body) as control, caddy(tmp_path, witness, control) as edge:
        status, headers, received = fetch(edge, "GET", "/v1/witness/change")
    assert status == 200 and headers["X-Stub"] == "witness"
    assert received == body
