"""Install-and-invoke oracle: start the REAL ``witness`` console script against a real witness
DB in a subprocess and confirm it serves the open nouns, and the signed ones only to an operator.

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
from types import SimpleNamespace

import pytest
from keri.app import habbing
from witness.changelog import ChangeLog, change_path


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
    log = ChangeLog.open(change_path(SimpleNamespace(name="testwit", base="", head_dir_path=head)))
    log.close_clean()
    return head, pre


def _operator_key(path):
    """An OpenSSH Ed25519 private key file, as `ssh-keygen -t ed25519 -N ''` writes one."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fiki import Key

    private = Ed25519PrivateKey.generate()
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.OpenSSH,
                                           serialization.NoEncryption()))
    return Key.from_seed(private.private_bytes_raw()).aid


def test_console_script_serves_the_versioned_control_plane(witness_db, tmp_path):
    from fiki import Key, Request as SignedRequest, sign_request, verify_response

    head, pre = witness_db
    port = _free_port()
    key = tmp_path / "operator"
    operator = _operator_key(key)
    watcher = Key.generate()
    proc = subprocess.Popen(
        [
            "uv", "run", "witness", "control-plane",
            "--name", "testwit",
            "--head-dir-path", head,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--no-telemetry",
            "--tag", "testnet",
            "--attrib", "operator=Bakobo",
            "--operator", operator,
            "--watcher", watcher.aid,
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
                status, health = _get_json(base + "/v1/witness/health")
                if status == 200:
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.3)
        assert health is not None and health["status"] == "ok", "the control plane never came up"

        info_status, info = _get_json(base + "/v1/witness/identity")
        assert info_status == 200
        assert info["aid"] == pre
        assert info["alias"] == "wit"
        assert info["change_key"].startswith("B")

        # This crosses argv, the read-only change log, and waitress serialization.
        feed_url = base + "/v1/witness/change?limit=2"
        feed_headers = sign_request(key=watcher, method="GET", url=feed_url)
        with urllib.request.urlopen(urllib.request.Request(feed_url, headers=feed_headers),
                                    timeout=5) as feed_response:  # noqa: S310 (localhost)
            feed_body = feed_response.read()
            verify_response(status=200, headers=feed_response.headers, body=feed_body,
                            request=SignedRequest("GET", feed_url, feed_headers),
                            expected_aid=info["change_key"], max_age=60, expected_keyid=None)
            feed = json.loads(feed_body)
        assert feed["change"]
        assert all(row["kel"] for row in feed["change"])

        # The tag has to survive the real argv -> door -> endpoint path, not just the unit one:
        # a flag the console script rejects at startup would make the container fail to come up,
        # and no unit test watches argv reach a running process.
        tag_status, claimed = _get_json(base + "/v1/witness/tags")
        assert tag_status == 200
        assert claimed == {"tags": ["testnet"], "source": "operator-config"}

        attr_status, published = _get_json(base + "/v1/witness/attribs")
        assert attr_status == 200
        assert published == {
            "attribs": {"operator": "Bakobo"},
            "source": "operator-config",
        }

        # @34sgjmxr through the shipped binaries: the signed nouns refuse curl and answer the
        # operator's `witness get`, so the flag, the guard and the signer all meet for real.
        with pytest.raises(urllib.error.HTTPError) as unsigned:
            _get_json(base + "/v1/witness/controller")
        assert unsigned.value.code == 401
        signed = subprocess.run(
            ["uv", "run", "witness", "get", "--key", str(key), base + "/v1/witness/controller"],
            capture_output=True, text=True, timeout=60,
        )
        assert signed.returncode == 0, signed.stderr
        assert "controllers" in json.loads(signed.stdout)
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # @79mupg4t through the real process: the request log reaches the process's stderr, a refused
    # read names nobody, and the operator's read names the operator.
    logged = [json.loads(line) for line in proc.stdout.read().decode("utf-8", "replace").splitlines()
              if line.startswith('{"event.name":"bakobo.control.request"')]
    by_outcome = {(entry["http.route"], entry["bakobo.control.outcome"]): entry for entry in logged}
    assert by_outcome[("/v1/witness/controller", "unauthenticated")]["enduser.id"] is None
    assert by_outcome[("/v1/witness/controller", "admitted")]["enduser.id"] == operator
    assert ("/v1/witness/health", "open") in by_outcome


def test_console_script_renders_a_landing_page():
    """@78m6fs3g. The installed `witness landing` entry point, stdin to stdout, with the default
    stylesheet loaded from the installed package rather than from the source tree."""
    document = json.dumps({
        "hostname": "w.example.com",
        "identity": {"aid": "B" + "A" * 43, "alias": "witness"},
        "tags": {"tags": ["testnet"], "source": "seed-file"},
        "attribs": {"attribs": {"operator": "Example"}, "source": "seed-file"},
    })
    result = subprocess.run(
        ["uv", "run", "witness", "landing"], input=document, capture_output=True, text=True,
        timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "w.example.com is a KERI witness." in result.stdout
    assert "--wl-accent" in result.stdout
