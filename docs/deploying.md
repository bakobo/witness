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

A keystore must exist in the volume before first start:

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

`Kevery.processQueryNotFound` walks the entire query-not-found escrow on **every** loop pass, roughly 32 times a second. A `/query` for an AID the witness does not hold parks an entry there, costing about four LMDB reads and four write-transaction attempts per pass until it ages out after `TimeoutQNF` (300 s). Sustaining N entries therefore costs an attacker only N/300 requests per second, and the per-pass cost of a deep escrow degrades the loop into slowness rather than unbounded growth.

That makes it a 300-second sliding window that drains on its own once a flood stops, which is why edge rate limiting genuinely helps here. It is a hardening observation rather than a keripy defect — `TimeoutQNF` exists to bound it — and it is **unmeasured**. Anything said about it upstream should carry a latency-versus-query-rate curve rather than a code reading.

## Upgrading

The image carries the keripy pin, so an upgrade is a digest change and a container restart over the same volume. Two things are not yet proven and should be before the first one that matters: bringing a new image up on an older image's LMDB without re-incepting, and whether a `kli migrate run` step is ever needed between pins. Neither has been exercised here.
