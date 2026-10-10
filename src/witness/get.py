"""``witness get``: one signed read of a control plane, for an operator (@34sgjmxr).

Six control-plane nouns answer only a request an operator signed (RFC 9421, via fiki), which curl
cannot produce. This is the smallest thing that can: it signs one GET with an operator's key and
prints the answer, so ``witness get --key k URL | jq`` replaces ``curl -s URL | jq``.

The key is an unencrypted OpenSSH Ed25519 private key, as ``ssh-keygen -t ed25519 -N '' -f k``
writes it, because that is the private-key format fiki reads. ``--aid`` prints the AID to name
with ``--operator`` and fetches nothing.
"""

from __future__ import annotations

import http.client
import json
import sys
import urllib.error
import urllib.request

import fiki

from .errors import (ControlIncomplete, ControlUnreachable, InvalidArguments, OperatorKeyUnusable,
                     ResponseTooLarge, WitnessError)

#: An OpenSSH Ed25519 private key is about 400 bytes; this is the size bound before the read.
MAX_KEY_BYTES = 8 * 1024
#: Bounded before it is trusted. A controller list is the largest answer and is far below this.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_TIMEOUT = 10.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so a signed request's headers go only where they were addressed.

    urllib follows a redirect with the original headers, Signature included, to whatever host the
    Location names, which hands a replayable signature to that host. A control plane never
    redirects, so a 3xx surfaces as the HTTPError it already is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_without_redirects(request, timeout):
    """Open ``request`` as urlopen would, except that no redirect is followed."""
    return _OPENER.open(request, timeout=timeout)


def _key(path) -> fiki.Key:
    try:
        with open(path, "rb") as handle:
            text = handle.read(MAX_KEY_BYTES + 1)
    except OSError as exc:
        raise OperatorKeyUnusable(f"I could not read the key at {path}: {exc.strerror}.") from None
    if len(text) > MAX_KEY_BYTES:
        raise OperatorKeyUnusable(f"The key at {path} is over {MAX_KEY_BYTES} bytes, so it is not "
                                  "an OpenSSH Ed25519 private key.")
    try:
        return fiki.Key.from_openssh(text.decode("utf-8"))
    except (fiki.FikiError, UnicodeError):
        # Not the exception's text: fiki's never quotes a private key, but a decode error can.
        raise OperatorKeyUnusable(
            f"The key at {path} is not an unencrypted OpenSSH Ed25519 private key. "
            "`ssh-keygen -t ed25519 -N '' -f <path>` makes one."
        ) from None


def _bounded(response) -> bytes:
    """The whole answer, refused when it is over the bound, cut short, or badly framed.

    A single bounded read returns short rather than raising when the peer closes early, so a
    declared Content-Length is checked against what arrived: a JSON prefix is not an answer.
    """
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (http.client.HTTPException, OSError) as exc:
        raise ControlIncomplete(f"The answer broke off while it was being read: {exc!r}.") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge(f"The answer is over {MAX_RESPONSE_BYTES} bytes, so I stopped "
                               "reading it.")
    declared = (getattr(response, "headers", None) or {}).get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) != len(body):
        raise ControlIncomplete(f"The answer said it was {declared} bytes and {len(body)} "
                                "arrived, so it was cut off.")
    return body


def _refusal(exc: urllib.error.HTTPError, body: bytes) -> str:
    """``<code>: <detail>`` from a problem document, or the bare status when there is none."""
    try:
        problem = json.loads(body)
        return f"{problem['code']}: {problem.get('detail', '')}"
    except (ValueError, TypeError, KeyError):
        return f"The control plane answered {exc.code} without a problem document."


def run(cfg, *, urlopen=open_without_redirects, out=None, err=None) -> int:
    """Print the answer to stdout and return 0, or explain on stderr and return 1."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        key = _key(cfg.key)
        if cfg.print_aid:
            out.write(f"{key.aid}\n")
            return 0
        try:
            headers = fiki.sign_request(key=key, method="GET", url=cfg.url)
            request = urllib.request.Request(cfg.url, headers=headers, method="GET")
        except (fiki.FikiError, ValueError) as exc:
            raise InvalidArguments(f"I cannot sign a request for that URL: {exc}") from None
        try:
            with urlopen(request, timeout=_TIMEOUT) as response:
                body = _bounded(response)
        except urllib.error.HTTPError as refused:
            body = _bounded(refused)
            out.write(body.decode("utf-8", "replace"))
            err.write(f"{_refusal(refused, body)}\n")
            return 1
        except http.client.HTTPException as exc:  # reached, but the reply was not HTTP
            raise ControlIncomplete(f"{cfg.url} answered with broken HTTP: {exc!r}.") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ControlUnreachable(f"I could not reach {cfg.url}: {exc}.") from None
    except WitnessError as exc:
        err.write(f"{exc.code}: {exc}\n")
        return 1
    out.write(body.decode("utf-8", "replace"))
    return 0
