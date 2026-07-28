"""Install-and-invoke oracle: start the REAL ``witness`` console script against a real witness
DB in a subprocess and confirm it serves /healthz and /info over HTTP.

This is an additional end-to-end oracle (ledger #19: a suite can pass while the shipped entry
point is unrunnable); it is deliberately not relied upon for coverage.
"""

import contextlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from keri.app import habbing


def _free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url, timeout=2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (localhost only)
        return resp.status, json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def witness_db(tmp_path):
    head = str(tmp_path / "keristore")
    hby = habbing.Habery(
        name="testwit", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hab = hby.makeHab(name="wit", transferable=False)
    pre = hab.pre
    hby.close()
    return head, pre


def test_console_script_serves_healthz_and_info(witness_db):
    head, pre = witness_db
    port = _free_port()
    proc = subprocess.Popen(
        [
            "uv", "run", "witness", "control-plane",
            "--name", "testwit",
            "--head-dir-path", head,
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"witness exited early ({proc.returncode}): "
                    f"{proc.stdout.read().decode('utf-8', 'replace')}"
                )
            try:
                status, health = _get_json(base + "/healthz")
                if status == 200:
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.3)
        assert health == {"status": "ok"}, "witness /healthz never came up"

        info_status, info = _get_json(base + "/info")
        assert info_status == 200
        assert info["aid"] == pre
        assert info["alias"] == "wit"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
