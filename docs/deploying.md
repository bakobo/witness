# Deploying a witness

Written for `bakobo/infra`, which consumes this repo's image. It states the contract: what to pull, what to run, what to publish, and the three things that are easy to get wrong and silent when you do.

## What to pull

`ghcr.io/bakobo/witness`, private, published from this repo's CI (`@lypmcw7f`, `@lnk24kwp`). Every build is tagged `sha-<full-commit>`; a release tag adds its `v<major>.<minor>.<patch>`. There is deliberately **no `latest`** — a floating tag is exactly the image-to-pin drift this arrangement exists to close.

**Deploy by digest, never by tag.** The publish workflow prints the digest to its job summary.

Take the digest from the run triggered by the **release tag**, not from the earlier `main` push. Container builds are not bit-reproducible — apt and pip layers carry timestamps — so the tag run rebuilds the same commit into a different digest and moves `sha-<commit>` onto it. Within that one run both tags point at the same image, so the tag run's digest is the one that agrees with `v<x.y.z>`. The image is built here rather than in `infra` because this repo owns the keripy pin: a direct git reference in `pyproject.toml`. Building anywhere else lets the image and the pin drift apart, which is the whole problem containerizing solves.

## What to run

One container, two processes, one PID namespace. A supervisor is PID 1; it starts `witness run` and `witness control-plane`, forwards `SIGTERM` to both, and applies an asymmetric failure policy — the control plane crashing restarts it and leaves the witness alone, the witness exiting takes the container down.

```
docker run -d --name witness \
    -v witness-data:/usr/local/var/keri \
    -p 127.0.0.1:5631:5631 \
    -p 127.0.0.1:5633:5633 \
    ghcr.io/bakobo/witness@sha256:<digest>
```

A keystore must exist in the volume before first start. `--nopasscode` leaves the witness's signing key **unencrypted at rest in the volume**, which is accepted deliberately: an encrypted keystore needs its passcode supplied at every start, so the passcode would have to live on the same host as the volume and would protect nothing against the threat that matters (host compromise). What does protect it is the volume's own access control. Do not copy the volume anywhere the host's uid boundary does not follow.

```
docker run --rm -v witness-data:/usr/local/var/keri --entrypoint kli \
    ghcr.io/bakobo/witness@sha256:<digest> init --name witness --nopasscode
```

| Port | What | Expose? |
| --- | --- | --- |
| 5631 | keripy witness HTTP | Yes, through Caddy (`infra @m4c35y`) |
| 5632 | keripy CESR-over-TCP | No — `@m4c35y` closes it, pending its own unverified caveat |
| 5633 | Bakobo control plane | **Loopback only.** Unauthenticated in this release |

The volume must persist. It holds the keystore, the AID, the KEL and every receipt; losing it loses the witness's identity, and a controller that designated it would have to rotate.

## Three things that are silent when wrong

**1. Publish to loopback, and put firewall rules in `DOCKER-USER`.** Docker's published-port rules are inserted in the `DOCKER` chain and **bypass `INPUT` entirely**, so `nftables`/`iptables` rules written in `INPUT` do not apply to a published container port. They will look configured and do nothing. `-p 127.0.0.1:...` is the confinement that actually holds; any rate limiting goes in `DOCKER-USER`.

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

**A keripy pin that moves forward past a migration.** keripy records a version in the database and refuses to open one that is behind the library, so the witness will not start. The control plane reports `e.self.config.migration.f` — permanent, not retryable, because no amount of waiting clears it:

```
docker run --rm -v witness-data:/usr/local/var/keri --entrypoint kli \
    ghcr.io/bakobo/witness@sha256:<new-digest> migrate run --name witness
```

Then start the container. `kli migrate list` shows what is outstanding and `kli migrate show` what has run.

**Going backwards — which mostly does not work.** Once a migration has run, the previous image cannot be deployed on that volume: keripy raises rather than opening it, and the control plane reports `e.self.config.rollback.f`. This is correct behaviour — the alternative is a newer database being read by code that does not understand it — but it makes an upgrade across a migration a **one-way door**, and infra's rollback plan cannot be "redeploy the previous digest".

So: **take a backup before any upgrade that changes the keripy pin**, and treat restoring it as the rollback path. Within a pin, rollback is just redeploying the previous digest and is free.

### Backing up

`witness backup` takes a **transactionally consistent** snapshot of every store using LMDB's own `env.copy`, from a read-only environment, **while the witness keeps running and keeps receipting**:

```
docker run -d --name witness -v witness-data:/usr/local/var/keri -v witness-backups:/backup \
    ghcr.io/bakobo/witness@sha256:<digest>
docker exec witness witness backup --name witness --to /backup/$(date +%F)
```

It prints a manifest, which is also written beside the copy. The manifest records the keripy version the backup was taken with, which is what makes the one-way door above checkable in advance rather than at restore time.

Two things it does that a `tar` of the volume does not. It is consistent rather than merely crash-recoverable — a tar taken while the witness runs may be missing the last transactions, and for a witness a missing receipt is a receipt a controller believes it has. And it copies **every** store: the signing keys live in the keystore, a separate LMDB from the event database, so a backup of `db` alone restores a witness that holds every event it ever receipted and cannot sign a single new one. `witness backup` refuses outright rather than writing a partial backup, because a partial backup is discovered during a restore.

One residual, stated because it is the interesting part. `env.copy` is atomic per LMDB environment and promises nothing across two, so copying three stores gives three instants and a backup taken from a running witness could in principle be internally torn. The harmful direction — a key history referencing a key the keystore does not hold, which signs perfectly and can never rotate — is closed by copying the keystore last, so it is never older than the database. And a witness's keystore is measurably static during operation, because a witness AID is non-transferable and cannot rotate. If a witness ever gains a rotating key, that trade wants revisiting: the stronger guarantee is to quiesce the stores under an exclusive lock, which costs the downtime this design exists to avoid.

Use a **named volume** for `/backup`. The image pre-creates and owns that directory, so Docker gives a fresh named volume the right ownership; a host bind mount does not inherit that and must be `chown 1001:1001` first.

### Restoring

Restore is a directory copy, which is why the backup mirrors the layout of the keri home. There is deliberately no `witness restore` subcommand: it would write into the witness's own volume, which is the one direction this design avoids, and a copy is something an operator can read before running it.

```
docker rm -f witness
docker volume rm witness-data && docker volume create witness-data
docker run --rm -v witness-data:/usr/local/var/keri -v witness-backups:/backup \
    --entrypoint sh ghcr.io/bakobo/witness@sha256:<digest> \
    -c 'cp -a /backup/<date>/. /usr/local/var/keri/'
docker run -d --name witness -v witness-data:/usr/local/var/keri ... 
```

Exercised end to end by `tests/test_image_upgrade.py`, which backs up a running witness, **destroys the volume**, restores from the backup alone, and then checks not only that the AID and witnessed key state came back but that the restored witness accepts and receipts a **new** event — the assertion that proves the signing keys came with it.

**Not yet proven:** no upgrade across an actual migration has been rehearsed, because the pin has not moved since this repo started building images. The mechanism above is verified — the version check, both refusals, and the same-pin replacement all have tests — but the migration itself has only been read, not run. Rehearse it in sandbox before the first real pin bump.
