"""Install-and-invoke oracle for `witness registrar`: start the REAL console script, publish a
snapshot as a configured publisher, subscribe an HTTP sink, and receive a signed batch.

Like tests/test_smoke.py, this exists because a suite can pass while the shipped entry point is
unrunnable, and it is not relied upon for coverage.
"""

import base64
import contextlib
import json
import socket
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fiki


def _free_port():
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(url, key, document, method="POST"):
    body = json.dumps(document).encode()
    headers = fiki.sign_request(key=key, method=method, url=url, body=body)
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as answer:  # noqa: S310 (loopback)
        return json.loads(answer.read())


def test_console_script_publishes_and_pushes_a_signed_batch(tmp_path):
    received = []
    arrived = threading.Event()

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((f"http://127.0.0.1:{sink.server_address[1]}{self.path}",
                             dict(self.headers), body))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            arrived.set()

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    publisher, observer = fiki.Key.generate(), fiki.Key.generate()
    port = _free_port()
    proc = subprocess.Popen(
        ["uv", "run", "witness", "registrar", "--store", str(tmp_path), "--port", str(port),
         "--publisher", publisher.aid, "--window", "0.3",
         "--allow-callback", "127.0.0.0/8"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        announced = json.loads(proc.stdout.readline())
        base = announced["url"]
        for _ in range(100):
            try:
                with contextlib.closing(socket.create_connection(("127.0.0.1", port), 0.1)):
                    break
            except OSError:
                threading.Event().wait(0.05)
        chain = base64.b64encode(b"rip+iss").decode()
        assert _post(f"{base}/v1/registrar/publication", publisher, {
            "registry": "EReg", "issuer": "EIss", "digest": "d1", "chain": chain,
            "kel": base64.b64encode(b"kel").decode()}) == {"digest": "d1"}
        callback = f"http://127.0.0.1:{sink.server_address[1]}/batch"
        answer = _post(f"{base}/v1/registrar/subscription", observer, {"callback": callback})
        assert answer["registrar"] == announced["aid"]
        assert arrived.wait(10), "no batch arrived within the window"
        url, headers, body = received[0]
        fiki.verify_request(method="POST", url=url, headers=headers, body=body, max_age=60,
                            expected_aid=announced["aid"])
        batch = json.loads(body)
        assert (batch["number"], batch["full"]) == (1, True)
        assert batch["heads"][0]["chain"] == chain
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        sink.shutdown()
