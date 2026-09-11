[![CI](https://github.com/bakobo/witness/actions/workflows/ci.yml/badge.svg)](https://github.com/bakobo/witness/actions/workflows/ci.yml)
[![Image](https://github.com/bakobo/witness/actions/workflows/image.yml/badge.svg)](https://github.com/bakobo/witness/actions/workflows/image.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

# witness

Bakobo's operator layer over a stock [keripy](https://github.com/WebOfTrust/keripy) witness — it makes a running witness observable **without forking keripy**.

A deployment is one container running two processes. `witness run` is the launcher: it imports keripy as a library, runs keripy's own witness doers unchanged, and adds one small doer that publishes loop telemetry. `witness control-plane` is a separate process that opens the witness's LMDB strictly read-only and serves an HTTP surface over it. The two are co-located, and that is load-bearing rather than convenient — see [`docs/deploying.md`](docs/deploying.md).

The design and its rationale live in `this.i` (the intent tree, the source of truth) and `docs/`.

## Requirements

- Python ≥ 3.14 (keripy's floor)
- [`uv`](https://docs.astral.sh/uv/)

Every dependency is public and pinned by commit, so `uv sync` resolves anonymously and needs no credential of any kind.

## From a fresh clone to passing tests

```sh
uv sync
uv run pytest
```

The suite runs under a **100% branch-coverage gate**. It includes an install-and-invoke smoke test that starts the real `witness` console script against a temporary witness database. A separate image oracle runs the built container end to end and is skipped unless you point it at an image:

```sh
docker build -t witness:dev .
WITNESS_IMAGE=witness:dev uv run pytest tests/test_image_smoke.py
```

There is also a load oracle that measures what a flood of unanswerable queries costs the witness — slow, gated behind `WITNESS_LOAD`, and documented with its measured curve in [`docs/escrow-load.md`](docs/escrow-load.md).

## Running it

The image is the supported deployment. It runs a supervisor as PID 1 which starts both processes and enforces an asymmetric failure policy: the control plane crashing restarts it and leaves the witness alone, while the witness exiting takes the container down so the orchestrator restarts the whole thing.

```sh
docker volume create witness-data
docker run --rm -v witness-data:/usr/local/var/keri --entrypoint kli \
    ghcr.io/bakobo/witness:<tag> init --name witness --nopasscode
docker run -d --name witness \
    -v witness-data:/usr/local/var/keri \
    -p 127.0.0.1:5631:5631 \
    -p 127.0.0.1:5633:5633 \
    ghcr.io/bakobo/witness:<tag>
```

Both processes can also be run directly:

```sh
witness run --name witness --alias witness --http 5631 --tcp 5632
witness control-plane --name witness --host 127.0.0.1 --port 5633
```

Add `--no-telemetry` to the control plane when the witness was started by stock `kli witness start`, which publishes none.

## Experimenting: a throwaway pool of witnesses

`witness pool` stands up N containers of this image, each with its own volume, keystore and published ports, and takes them all away again. It runs on a host with Docker rather than inside the image, and it is for experiments — `toad` behaviour, a witness going away, a credential ceremony against witnesses that genuinely receipt:

```sh
export WITNESS_IMAGE=witness:dev
uv run witness pool up --name lab --count 3
uv run witness pool status --name lab
uv run witness pool manifest --name lab --format heti   # the standing witness set, for a lockbox
uv run witness pool break --name lab --witness w2       # and `heal` to put it back
uv run witness pool down --name lab                     # removes every container and volume
```

The pool serves witnesses and describes them; it never incepts anything, so designating it is [`heti`](https://github.com/bakobo/heti)'s or `kli`'s job. Full documentation, including the reproducible-`--seed` caveat and what a pool is *not* good for, is in [`docs/pools.md`](docs/pools.md).

And a witness can be backed up without being stopped:

```sh
witness backup --name witness --to /backup/2026-09-04
```

That takes a transactionally consistent snapshot of every store — including the keystore, where the signing keys live — using LMDB's own `env.copy` from a read-only environment. See [`docs/deploying.md`](docs/deploying.md) for the restore procedure, which is a directory copy.

## The control-plane surface

Every path is `/v1/witness/<noun>`. All are `GET`, and all are unauthenticated in this release — see the note below.

| Path | What it answers |
| --- | --- |
| `/v1/witness/health` | Whether the witness is *working*: the database opens **and** the hio loop is not stuck inside a doer. |
| `/v1/witness/identity` | The witness's own AID and alias. |
| `/v1/witness/version` | This package and the keripy it wraps. |
| `/v1/witness/loop` | Loop ticks, lag against the tock, the doer currently executing, and per-doer timings. |
| `/v1/witness/escrow` | Depth of every escrow store, query-not-found first. |
| `/v1/witness/database` | Size against keripy's fixed 100 MB map ceiling, and the registered reader count. |
| `/v1/witness/process` | CPU, memory, threads and descriptors for the witness process. |
| `/v1/witness/controller` | Every controller whose key state this witness holds. |
| `/v1/witness/controller/{aid}` | One controller's key state, or `404`. |

Health is worth a sentence. A witness whose loop has wedged still has a perfectly openable database, so a probe that only opens the database stays green through the failure that has actually happened. This one reports `degraded` and names the doer, but only once that doer has held the loop past five seconds: a sample catching the loop mid-doer is normal, and treating it as a wedge would make the probe a random alarm.

## Metrics

The control plane exports OpenTelemetry metrics over OTLP when a collector endpoint is configured, and does nothing at all when one is not:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 witness control-plane ...
```

Series cover witness liveness, loop lag and per-doer maxima, escrow depth per store, database usage, process vitals, and control-plane request counts by route template and status. There is no scrape endpoint, deliberately: `bakobo/dev`'s `ops.md` §7 puts the OpenTelemetry SDK in code Bakobo writes and OTLP on the wire.

## Errors

Failures are RFC 9457 problem documents with media type `application/problem+json`:

```json
{
  "code": "e.env.witnessdb.unavailable.r",
  "type": "https://errors.bakobo.com/e.env.witnessdb.unavailable.r",
  "title": "I could not open the witness database.",
  "detail": "The witness database was not found at the configured location; the witness may not be running yet.",
  "instance": "/v1/witness/identity",
  "request_id": "…"
}
```

The code carries the meaning: it is classified by what the obstacle was rather than by which component raised it, and the trailing token is the disposition — `.r` means retrying could help, `.f` means it will not. The HTTP status follows from the code's prefix, so two endpoints can never disagree about the same condition.

## Authentication

This release is unauthenticated, which is why the control-plane port must be bound to loopback and reached through the estate's reverse proxy rather than exposed. Signed requests (RFC 9421, via [`fiki`](https://github.com/bakobo/fiki)) are the next phase and are required before any endpoint that changes anything.

## License

Apache-2.0.
