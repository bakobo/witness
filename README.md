[![CI](https://github.com/bakobo/witness/actions/workflows/ci.yml/badge.svg)](https://github.com/bakobo/witness/actions/workflows/ci.yml)

# witness

Bakobo's operator layer over a stock [keripy](https://github.com/WebOfTrust/keripy) witness — it
makes a running witness observable **without forking keripy**.

A deployment has two cooperating pieces: a witness *runner* (later) and a separate, read-only
*control-plane* process. This first slice ships the **control-plane reader** — a standalone process
that opens the witness's LMDB database strictly **read-only** (so it can never stall or corrupt the
witness it observes) and serves liveness and identity over HTTP.

The design and its rationale live in `this.i` (the intent tree, the source of truth) and `docs/`.

## Requirements

- Python ≥ 3.14 (keripy's floor)
- [`uv`](https://docs.astral.sh/uv/)

## From a fresh clone to passing tests

```sh
uv sync
uv run pytest
```

`uv sync` installs the pinned keripy plus `falcon` and `waitress`; `uv run pytest` runs the suite
under a **100% branch-coverage gate** (`--cov-fail-under=100`). The suite includes an
install-and-invoke smoke test that starts the real `witness` console script against a temporary
witness database and exercises the HTTP endpoints end to end.

## Running the control plane

```sh
witness control-plane --name <keystore-name> [--base <base>] [--head-dir-path <path>] \
    --host 127.0.0.1 --port 5666
```

| Flag              | Required | Default       | Meaning                                             |
| ----------------- | -------- | ------------- | --------------------------------------------------- |
| `--name`          | yes      | —             | The witness keystore/database name.                 |
| `--base`          | no       | `""`          | The keystore base subdirectory.                     |
| `--head-dir-path` | no       | keripy's default | The keystore head directory.                     |
| `--host`          | no       | `127.0.0.1`   | The interface to bind.                               |
| `--port`          | yes      | —             | The TCP port to bind.                                |

### Endpoints (unauthenticated in this phase)

- `GET /healthz` — liveness. `200 {"status": "ok"}` when the witness database opens read-only;
  `503 {"status": "unavailable", "error": {…}}` when it cannot.
- `GET /info` — the witness's own identity:
  `200 {"aid": …, "alias": …, "keripy_version": …, "db_path": …}`; `503` with the error body when
  the database (or the witness identity) is unavailable.

Request authentication (RFC 9421 / HTTP Message Signatures) is deferred to a later phase; these P0
endpoints are unauthenticated.

## Errors

Failures are typed and carry a stable symbolic code, a plain-sentence message, and a `retryable`
flag (transient vs. permanent). Error responses use the shape
`{"code": …, "message": …, "retryable": …}`. A database that will not open is `retryable: true` —
the witness may simply not be running yet.
