[![CI](https://github.com/bakobo/witness/actions/workflows/ci.yml/badge.svg)](https://github.com/bakobo/witness/actions/workflows/ci.yml)
[![Image](https://github.com/bakobo/witness/actions/workflows/image.yml/badge.svg)](https://github.com/bakobo/witness/actions/workflows/image.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

# witness

Bakobo's operator layer over a stock [keripy](https://github.com/WebOfTrust/keripy) witness — it makes a running witness observable and deployable on the public internet **without forking keripy**.

A deployment is one container running two processes. `witness run` imports keripy as a library, runs keripy's own witness doers unchanged, and adds loop telemetry and a change log of accepted events and receipts. `witness control-plane` opens the witness's database strictly read-only and serves an HTTP surface over it: health, escrow depth, key state, and a signed change feed for watchers.

To see one running, Bakobo has a [trial witness](https://wit1.try.bakobo.com/) open for evaluation, and publishes its production pool at [`bakobo.com/.well-known`](https://bakobo.com/.well-known/index.html) — see [`docs/discovery.md`](docs/discovery.md).

## From a fresh clone to passing tests

You need Python ≥ 3.14 (keripy's floor) and [`uv`](https://docs.astral.sh/uv/). Every dependency is public and pinned by commit, so `uv sync` resolves anonymously and needs no credential of any kind.

```sh
uv sync
uv run pytest
```

The suite runs under a **100% branch-coverage gate**. It includes a smoke test that starts the real `witness` console script against a temporary witness database, and `tests/test_doors.py`, a census that fails if any boundary read in `src/witness` bypasses a named input door.

Some tests run shipped artifacts rather than imported code, and skip unless pointed at one: the image oracles need `WITNESS_IMAGE`, the deploy-kit proxy test needs a `caddy` binary, and the load oracle in [`docs/escrow-load.md`](docs/escrow-load.md) needs `WITNESS_LOAD`.

## Working with Docker

Build the image and run the oracles against it:

```sh
docker build -t witness:dev .
WITNESS_IMAGE=witness:dev uv run pytest tests/test_image_smoke.py tests/test_image_upgrade.py --no-cov
```

Published images are at `ghcr.io/bakobo/witness`, pull anonymously, and carry no `latest` tag. Deploy by digest, after verifying its signature. To run one, create a keystore in a volume and start it under the hardened flags:

```sh
HARDENED="--read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m --cap-drop ALL --security-opt no-new-privileges --pids-limit 128"
docker volume create witness-data
docker run --rm $HARDENED -v witness-data:/usr/local/var/keri --entrypoint kli \
    witness:dev init --name witness --nopasscode
docker run -d --name witness $HARDENED \
    -v witness-data:/usr/local/var/keri \
    -p 127.0.0.1:5631:5631 -p 127.0.0.1:5633:5633 \
    witness:dev
curl -s http://127.0.0.1:5633/v1/witness/health
```

PID 1 is a supervisor: the control plane crashing restarts it and leaves the witness alone, while the witness exiting takes the container down so the orchestrator restarts the whole thing.

For a local set of several witnesses to experiment with, `uv run witness pool up --name lab --count 3` stands them up and `witness pool down` removes them; see [`docs/pools.md`](docs/pools.md).

## Running it for real

The operator's guide starts at [`docs/README.md`](docs/README.md). In brief:

- [`docs/deploying.md`](docs/deploying.md) — verifying a digest, running it, alerting, upgrading, backup and restore.
- [`deploy/`](deploy/README.md) — a reference kit for the edge a public witness needs: Caddy, nftables, fail2ban.
- [`docs/hardening.md`](docs/hardening.md) — what has been hardened, what proves it, and what it does not cover.
- [`docs/control-plane.md`](docs/control-plane.md) — the HTTP surface, authentication, metrics and errors.

The design and its rationale live in [`this.i`](this.i), the intent tree that is this repo's source of truth.

## License

Apache-2.0.
