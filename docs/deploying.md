# Deploying a witness

Written for the deployment that consumes this repo's image. It states the contract: what to pull, what to run, what to publish, and the three things that are easy to get wrong and silent when you do.

[`deploy/`](../deploy/README.md) is a reference kit built on this contract: a hardened compose file, a Caddyfile, nftables limits and a fail2ban jail, with how to install each and how to prove it is live (`@2hnvwmg2`).

## What to pull

`ghcr.io/bakobo/witness`, published from this repo's CI (`@lypmcw7f`, `@lnk24kwp`). The package is **public** and pulls anonymously — a credential is needed to push, not to pull. Every build is tagged `sha-<full-commit>`; a release tag adds its `v<major>.<minor>.<patch>`. There is deliberately **no `latest`** — a floating tag is exactly the image-to-pin drift this arrangement exists to close.

**Deploy by digest, never by tag.** The publish workflow prints the digest to its job summary.

Take the digest from the run triggered by the **release tag**, not from the earlier `main` push. Container builds are not bit-reproducible — apt and pip layers carry timestamps — so the tag run rebuilds the same commit into a different digest and moves `sha-<commit>` onto it. Within that one run both tags point at the same image, so the tag run's digest is the one that agrees with `v<x.y.z>`. The image is built here rather than in the deployment repository because this repo owns the keripy pin: a direct git reference in `pyproject.toml`. Building anywhere else lets the image and the pin drift apart, which is the whole problem containerizing solves.

## Verify before you run a digest

Every digest pushed from `main` or a `v*` tag is signed keyless with cosign, and carries a SLSA build provenance attestation and an SPDX SBOM, all bound to the digest (`@4dx8zyjr`). Pull requests are never signed. **Run this before starting any digest, and refuse to start it if it fails:**

```
cosign verify ghcr.io/bakobo/witness@sha256:<digest> \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github\.com/bakobo/witness/\.github/workflows/image\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$'
```

The two pins are the whole check. The issuer says the certificate was minted against a GitHub Actions OIDC token; the identity says the workflow that held that token was this repository's `image.yml`, running on a release tag. It deliberately refuses a digest signed from a `main` push, a pre-release tag such as `v1.2.3-rc1`, any other workflow, and any fork: production deploys the release-tag digest (above), so a signature from anywhere else is not one a deployment should accept. Leaving out either pin, or loosening the identity to a prefix, accepts a signature from anyone's workflow. `tests/test_supply_chain.py` holds this command to those pins. The signing job runs the same verification against every digest it signs, with its own ref as the identity, so a release whose signature does not verify fails its own build.

cosign v3 needs no credential: the package pulls anonymously and the trust root is Sigstore's public-good instance, so the deploying host needs outbound HTTPS to Sigstore (the TUF root, Rekor) and to GHCR. Install cosign by a pinned checksum, as `image.yml` does, rather than from a moving package.

Provenance and the SBOM are for audit rather than for the gate:

```
gh attestation verify oci://ghcr.io/bakobo/witness@sha256:<digest> --repo bakobo/witness
gh attestation verify oci://ghcr.io/bakobo/witness@sha256:<digest> --repo bakobo/witness \
    --predicate-type https://spdx.dev/Document/v2.3
```

## What to run

One container, two processes, one PID namespace. A supervisor is PID 1; it starts `witness run` and `witness control-plane`, forwards `SIGTERM` to both, and applies an asymmetric failure policy — the control plane crashing restarts it and leaves the witness alone, the witness exiting takes the container down.

Run it hardened (`@4hwwhshr`). Every `docker run` in this document passes the same flags, so define them once:

```
HARDENED="--read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m --cap-drop ALL --security-opt no-new-privileges --pids-limit 128"
```

The root filesystem is read-only, every capability is dropped, setuid cannot regain one, and the process table is bounded at 128 tasks against a measured peak of 14. Three paths stay writable, and only three: the data volume at `/usr/local/var/keri`, `/backup` where backups are taken, and `/tmp` as a small tmpfs. The tmpfs is not optional — hio resolves a temporary directory when it is imported, so without it `import keri` fails and nothing starts, `kli init` included. **Never mount the data volume read-only**: LMDB then silently drops its lock file and readers lose data (see the second silent failure below). `tests/test_image_smoke.py`, `tests/test_image_upgrade.py` and the pool oracle run every container under exactly these flags, and one test reads the result from inside the running container, so CI proves the image works hardened. A test also holds this document to the flag set in `witness.hardening.RUN_FLAGS`. In a systemd unit, which has no shell, write the flags out in full.

```
docker run -d --name witness $HARDENED \
    -v witness-data:/usr/local/var/keri \
    -p 127.0.0.1:5631:5631 \
    -p 127.0.0.1:5633:5633 \
    -e WITNESS_OPERATORS=<operator AID> \
    ghcr.io/bakobo/witness@sha256:<digest>
```

`WITNESS_OPERATORS` names who may read the six signed control-plane nouns; `witness get --key <key> --aid` prints an operator's AID. Leave it out and those nouns refuse everyone while health and identity keep answering. [Authentication](../README.md#authentication) has the rest.

Set `WITNESS_WATCHERS=<watcher AID>[,<watcher AID>...]` to grant the separate change feed. Leave it unset to refuse all feed reads. The runner writes accepted events and stored receipts to a separate LMDB change log under the witness volume's `change/` directory. Keep that directory across ordinary restarts: a clean runner restart preserves the log generation and its response signing key, so watchers can continue polling. An unclean exit or failed log write starts a new generation, which makes watchers begin an initial sync. The cursor's hash chain also detects a restored log whose entries diverge from what a watcher previously read. `witness backup` omits the change log; restoring that backup creates a new log and response key that watchers must pin again.

A keystore must exist in the volume before first start. `--nopasscode` leaves the witness's signing key **unencrypted at rest in the volume**, which is accepted deliberately: an encrypted keystore needs its passcode supplied at every start, so the passcode would have to live on the same host as the volume and would protect nothing against the threat that matters (host compromise). What does protect it is the volume's own access control. Do not copy the volume anywhere the host's uid boundary does not follow.

```
docker run --rm $HARDENED -v witness-data:/usr/local/var/keri --entrypoint kli \
    ghcr.io/bakobo/witness@sha256:<digest> init --name witness --nopasscode
```

| Port | What | Expose? |
| --- | --- | --- |
| 5631 | keripy witness HTTP | Yes, through Caddy |
| 5632 | keripy CESR-over-TCP | No — `@m4c35y` closes it, pending its own unverified caveat |
| 5633 | Bakobo control plane | **Loopback only.** Six nouns need a signed request from an operator named by `WITNESS_OPERATORS` |

The volume must persist. It holds the keystore, the AID, the KEL and every receipt; losing it loses the witness's identity, and a controller that designated it would have to rotate.

## Three things that are silent when wrong

**1. Publish to loopback, and rate-limit where public traffic actually arrives.** Docker inserts its published-port rules in the `DOCKER` chain, which hangs off `FORWARD` and **bypasses `INPUT` entirely**, so a container port published to the world is not reached by `nftables`/`iptables` rules written in `INPUT`. They will look configured and do nothing. `-p 127.0.0.1:...` is the confinement that actually holds. With every port on loopback and Caddy running as a host process in front of 5631, public traffic terminates at Caddy, a local socket, so it traverses `INPUT` and never touches the `DOCKER` chain. Per-client-IP limits therefore belong in an `nftables` table of your own hooked on `input`, matched on Caddy's public ports; rules in `DOCKER-USER` would never see a public client, and at best Caddy's own loopback hop to the container, where every client looks like 127.0.0.1. `DOCKER-USER` is the right place only if you publish a container port publicly instead, which this guide does not recommend.

**2. Do not split the two processes across containers.** It looks like better isolation and it is broken. LMDB's reader-registration protocol takes an `fcntl` byte-range lock at an offset equal to the reader's own pid, which assumes one PID namespace; two containers both running their process as pid 1 collide, and the control plane fails to open the database at all. Mounting the database read-only appears to fix that and makes it far worse — LMDB silently omits its lock file on a read-only filesystem, the reader stops publishing its snapshot id, and the writer recycles pages underneath it. Measured: a reader lost all 2000 keys it had just read, inside its own transaction, with no error raised. `@a24p3kbw` has the full account; the evidence is in `.ignored/lmdb-two-container-analysis.md`.

**3. Size the container for the database, not for the interpreter.** The LMDB mapping is file-backed and shared, so RSS is not what the container pays. Measured: a reader scanning a 200 MB database grew RSS by 200 MB and its cgroup charge by 0.5 MB, while the writer's cgroup carried all of it. Kernel cgroup-v2 calls ownership of a shared mapping "in-deterministic", so the two processes cannot be sized independently — and because keripy does not use `MDB_WRITEMAP`, write bursts land as *dirty* page cache, which reclaim cannot simply drop. A limit tight enough to force reclaim during a burst throttles the witness rather than killing it, which is harder to diagnose than a crash. 128 MB is safe for a small database; give it headroom as the database grows toward its 100 MB ceiling.

## Health and alerting

`GET /v1/witness/health` is the probe. It returns `200 {"status": "ok", "ticks": N}` when the database opens **and** the hio loop is not stuck, and `{"status": "degraded", "reason": "the loop has been inside <doer> for <N>s"}` when one doer has held it past five seconds. Presence of a current doer is NOT a wedge — on a healthy loop a sample catches it inside some doer most of the time — so the check measures duration. A witness whose loop has wedged still opens its database perfectly, so a probe that only checks the database stays green through the failure that has actually happened — this is `ops.md` §7's "prove the service is working, not that a port is open", made specific.

Metrics leave over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and are silent otherwise. The series worth alerting on:

| Metric | Why |
| --- | --- |
| `witness.up` | 0 while degraded, absent while unreachable — an alert can tell those apart |
| `witness.loop.lag` | Direct measure of the harm in a single-threaded cooperative loop |
| `witness.escrow.depth{store="query_not_found"}` | The one observable for a query flood; see below |
| `witness.database.used_fraction` | keripy's map ceiling is fixed at 100 MB and a witness that reaches it stops accepting events |
| `witness.loop.doer.max_seconds{doer=…}` | Names the doer when the loop is slow |
| `witness.controlplane.requests{route,status}` | Sets a rate limit from evidence rather than a guess |

Do not alert on `witness.process.*` thresholds. They are for diagnosis after an alert fires, and `ops.md` §7 is explicit that CPU and memory thresholds carrying no action just train their recipient to ignore the channel.

### The request log

The control plane writes one JSON line to stderr for every request it answers, so `docker logs`, the unit's journal and anything shipping that journal all carry it (`this.i` `79mupg4t`). Every line has exactly these keys, null where one does not apply:

| Key | Value |
| --- | --- |
| `event.name` | Always `bakobo.control.request`, which tells these lines from keripy's output on the same stream |
| `bakobo.request.id` | The `Bakobo-Request-Id` the response carried, so a problem document's `request_id` finds its line |
| `http.request.method` | A standard HTTP verb, or `_OTHER` |
| `http.route` | The route template, such as `/v1/witness/controller/{aid}`; null for an unknown path |
| `http.response.status_code` | The status, as an integer |
| `bakobo.control.outcome` | `open` for an unsigned noun, else `admitted`, `unauthenticated` or `denied`; null when no endpoint ran |
| `error.type` | The error code, such as `e.grant.missing.operator.f` |
| `enduser.id` | The signer's AID, only when the signature verified |

A refused request names a signer only when the signature was genuine and the signer is not an operator. An unverified claim to be somebody is never logged, and neither are signatures, keys, raw paths or query strings.

```
{"event.name":"bakobo.control.request","bakobo.request.id":"3f2a9c0d1e7b4a6f8c5d2e1f0a9b8c7d","http.request.method":"GET","http.route":"/v1/witness/controller/{aid}","http.response.status_code":403,"bakobo.control.outcome":"denied","error.type":"e.grant.missing.operator.f","enduser.id":"BHJrH2KGtjVszHvCLhj9oiXMnLyOllyRnR9tlsrsGLL7"}
```

### The query-not-found escrow, specifically

**Measured, not inferred** — see [`escrow-load.md`](escrow-load.md) for the curve and the harness.

`Kevery.processQueryNotFound` walks the entire query-not-found escrow on every hio loop pass. A `/query` for an AID the witness does not hold parks an entry there for `TimeoutQNF` (300 s), and queries are not authenticated at the public port, so sustained depth is roughly the attacker's request rate times 300.

The cost is linear at about 0.35 ms per entry per pass, paid by `WitnessStart` — the same doer that ingests and receipts real events. Measured: at 200 entries a controller waits 0.075 s to be witnessed instead of 0.046 s; at 1,000 entries, 0.257 s, with the loop running 0.33 s behind a 0.03125 s tock.

Two things follow that the arithmetic alone did not give. **Degradation starts under one request per second** — around 200 entries — which is one to two orders of magnitude below the "10,000 entries at 33 req/s" the depth figure suggests. And **it drains on its own**: during the run, depth fell from 1,000 to 823 while the flood continued, because entries were ageing out faster than they arrived. A flood that stops is a witness that recovers unattended.

So a per-source rate limit is worth having and is not sufficient. A limit loose enough for legitimate traffic — a witness serves KELs to strangers on demand — still admits one source at 1 req/s, and a hundred sources at 0.01 req/s each is invisible to it.

**The image also paces the sweep.** `witness run --escrow-interval` defaults to **1 second** where keripy sweeps every escrow on every hio pass, 32 times a second (`@zj3h2pzh`). Measured at 5 req/s of attack: median loop lag falls from 0.079 s to **zero**, with the same escrow depth. `--escrow-interval 0` restores keripy's own behaviour exactly.

**The image already shortens the window.** `witness run --escrow-timeout` defaults to **60 seconds** where keripy's own default is 300 (`@znm5uppx`). Measured: at 5 req/s the escrow pins at 301 entries with 0.077 s of lag, against ~1,000 entries and 0.34 s at keripy's default. Raise it if a controller population genuinely queries ahead of its own events; lower it to trade more of that tolerance for less exposure.

**Alert on `witness.loop.lag` above 0.05 s sustained for a minute**, which is where the measurement puts the onset of noticeable delay, and read `witness.escrow.depth{store="query_not_found"}` alongside it: lag rising with a flat escrow depth is something else, such as a slow disk.

`/v1/witness/health` will **not** tell you. It reports `ok` at 0.33 s of lag, because health answers "is this witness working" and a degraded witness is working. That split is deliberate and pinned by a test.

## Upgrading

An upgrade is a digest change and a container replacement over the same volume. What happens next depends entirely on whether the new image's keripy agrees with the version recorded in the database, so there are three cases and they are not symmetric.

**Same keripy pin — the ordinary case.** Replace the container, keep the volume. The witness keeps its AID, its KEL and every receipt, and goes on witnessing. This is exercised end to end by `tests/test_image_upgrade.py`, which starts a container, gets a controller's inception accepted, replaces the container entirely, and then checks both that the state survived *and* that the new container still accepts a further event — the second being the part that separates "the data is on disk" from "the witness works".

Point that oracle at the digest you are upgrading **from** and it proves the thing an upgrade actually claims rather than a proxy for it:

```
WITNESS_IMAGE=witness:candidate \
WITNESS_BASELINE_IMAGE=ghcr.io/bakobo/witness@sha256:<deployed> \
    uv run pytest tests/test_image_upgrade.py -v --no-cov
```

With a baseline set, the deployed image creates the volume, initialises the keystore and gets a controller witnessed; the candidate takes the volume over and has to serve the same AID, return the same key state, and accept a further event — and then the deployed image takes it **back**, which is the rollback this document promises two paragraphs down. Without one, those two tests skip and say so; they deliberately do not fall back to using the candidate as its own baseline, because that proves container replacement while reading as version compatibility (`@ktljhcyt`). In CI the baseline comes from the `DEPLOYED_WITNESS_IMAGE` repository variable, which the deployment updates when it bumps its pin.

**A keripy pin that moves forward past a migration.** keripy records a version in the database and refuses to open one that is behind the library, so the witness will not start. The control plane reports `e.self.config.migration.f` — permanent, not retryable, because no amount of waiting clears it:

```
docker run --rm $HARDENED -v witness-data:/usr/local/var/keri --entrypoint kli \
    ghcr.io/bakobo/witness@sha256:<new-digest> migrate run --name witness
```

Then start the container. `kli migrate list` shows what is outstanding and `kli migrate show` what has run.

**Going backwards — which mostly does not work.** Once a migration has run, the previous image cannot be deployed on that volume: keripy raises rather than opening it, and the control plane reports `e.self.config.rollback.f`. This is correct behaviour — the alternative is a newer database being read by code that does not understand it — but it makes an upgrade across a migration a **one-way door**, and the deployment's rollback plan cannot be "redeploy the previous digest".

So: **take a backup before any upgrade that changes the keripy pin**, and treat restoring it as the rollback path. Within a pin, rollback is just redeploying the previous digest and is free.

### Backing up

`witness backup` takes a **transactionally consistent** snapshot of every keripy store using LMDB's own `env.copy`, from a read-only environment, **while the witness keeps running and keeps receipting**. It omits the separate change log:

```
docker run -d --name witness $HARDENED \
    -v witness-data:/usr/local/var/keri -v witness-backups:/backup \
    ghcr.io/bakobo/witness@sha256:<digest>
docker exec witness witness backup --name witness --to /backup/$(date +%F)
```

It prints a manifest, which is also written beside the copy. The manifest records the keripy version the backup was taken with, which is what makes the one-way door above checkable in advance rather than at restore time.

Two things it does that a `tar` of the volume does not. It is consistent rather than merely crash-recoverable — a tar taken while the witness runs may be missing the last transactions, and for a witness a missing receipt is a receipt a controller believes it has. And it copies **every keripy store**: the witness signing keys live in the keystore, a separate LMDB from the event database, so a backup of `db` alone restores a witness that holds every event it ever receipted and cannot sign a single new one. `witness backup` refuses outright rather than writing a partial backup of those stores, because a partial backup is discovered during a restore. The change log is created anew after restore; its old cursors and response key are no longer valid.

One residual, stated because it is the interesting part. `env.copy` is atomic per LMDB environment and promises nothing across two, so copying three stores gives three instants and a backup taken from a running witness could in principle be internally torn. The harmful direction — a key history referencing a key the keystore does not hold, which signs perfectly and can never rotate — is closed by copying the keystore last, so it is never older than the database. And a witness's keystore is measurably static during operation, because a witness AID is non-transferable and cannot rotate. If a witness ever gains a rotating key, that trade wants revisiting: the stronger guarantee is to quiesce the stores under an exclusive lock, which costs the downtime this design exists to avoid.

Use a **named volume** for `/backup`. The image pre-creates and owns that directory, so Docker gives a fresh named volume the right ownership; a host bind mount does not inherit that and must be `chown 1001:1001` first.

### Restoring

Restore is a directory copy, which is why the backup mirrors the layout of the keri home. There is deliberately no `witness restore` subcommand: it would write into the witness's own volume, which is the one direction this design avoids, and a copy is something an operator can read before running it.

```
docker rm -f witness
docker volume rm witness-data && docker volume create witness-data
docker run --rm $HARDENED -v witness-data:/usr/local/var/keri -v witness-backups:/backup \
    --entrypoint sh ghcr.io/bakobo/witness@sha256:<digest> \
    -c 'cp -a /backup/<date>/. /usr/local/var/keri/'
docker run -d --name witness $HARDENED -v witness-data:/usr/local/var/keri ...
```

Exercised end to end by `tests/test_image_upgrade.py`, which backs up a running witness, **destroys the volume**, restores from the backup alone, and then checks not only that the AID and witnessed key state came back but that the restored witness accepts and receipts a **new** event — the assertion that proves the signing keys came with it.

The restored runner creates a fresh change log when it starts. Watchers must pin the new `change_key` from `/v1/witness/identity` and omit `since` to begin a full initial sync.

**Where this stands, and the pin HAS moved.** As of 2026-09-21 the deployed digest carries keripy `2.0.0-dev6` while `main` carries `2.1.0-dev1`, so the next release to production crosses a migration — this is no longer the hypothetical it was when this section was written. The mechanism is verified by tests: the version check, both refusals, and the same-pin handover in **both directions between two real digests**. The migration itself is **not** exercised by anything here. The oracle declines to guess about it — when the two images carry different keripy versions it skips and names both, because a failure there would read as a broken upgrade when what happened is that it was pointed at a pin bump.

It has been rehearsed by hand, on synthetic data, and this is what was measured: the new image refuses the old database and the container crash-loops, `kli migrate run` completes, the witness then keeps its AID and its controllers and receipts a new event, and the old image afterwards refuses the migrated database for good. Two claims made above did **not** survive that rehearsal and are corrected here: the control plane does not report `e.self.config.migration.f` when this happens — it is in the same container, which is down — and `kli migrate list` fails with the same `DatabaseError` rather than listing what is outstanding. The diagnosis is `docker logs`.
