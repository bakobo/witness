# Measured: what an unanswerable-query flood costs a witness

Measured 2026-09-04 against `bakobo/witness` at `b7c89e4`, keripy `2.0.0-dev6` (pin `366d8107`), in the shipped container image on an 8-core Linux host. Harness: `tests/test_escrow_load.py`; the exploratory run that produced the table is the same procedure with more sample points.

```sh
GH_TOKEN=$(gh auth token) docker build --secret id=gh_token,env=GH_TOKEN -t witness:load .
WITNESS_IMAGE=witness:load WITNESS_LOAD=1 uv run pytest tests/test_escrow_load.py
```

It takes about seven minutes, most of it waiting out `TimeoutQNF` to watch the escrow drain, which is why it is gated behind `WITNESS_LOAD` rather than running with the other image oracles.

This exists because the repo had been asserting something it had not measured. `docs/deploying.md` and the v0.1.0-rc review panel (SEC-F1) both described this vector from a code reading and both said, correctly, that a code reading is not enough to act on — and that nothing should go upstream without a curve. Here is the curve.

## The mechanism, briefly

`Kevery.processQueryNotFound` walks the **entire** query-not-found escrow on every hio loop pass. A `/query` for an AID the witness does not hold parks an entry there; each query carries a distinct SAID, so entries accumulate one per request. They age out after `TimeoutQNF`, which is 300 seconds. Queries are not authenticated at the witness's public HTTP port.

So sustained depth ≈ attacker request rate × 300, and the cost is paid on every pass by the doer that also does the witness's real work.

## The curve

| qnfs depth | implied attacker req/s | loop lag (s) | `WitnessStart` per pass (s) | a real controller is witnessed in (s) |
| --- | --- | --- | --- | --- |
| 0 | 0.0 | 0.0000 | 0.0002 | 0.046 |
| 50 | 0.17 | 0.0022 | 0.0209 | 0.050 |
| 100 | 0.33 | 0.0034 | 0.0345 | 0.067 |
| 200 | 0.67 | 0.0654 | 0.0964 | 0.075 |
| 400 | 1.33 | 0.1117 | 0.1428 | 0.087 |
| 600 | 2.0 | 0.1801 | 0.2111 | 0.235 |
| 1000 | 3.33 | 0.3345 | 0.3654 | 0.257 |

The last column is the number that matters: wall-clock time for a fresh controller's inception to be accepted and appear in this witness's key state, which is the service the witness exists to provide.

## What it says

**The cost is linear in depth, at roughly 0.35 ms per escrowed entry per pass.** `WitnessStart` — the doer that ingests and receipts real events — goes from 0.2 ms to 365 ms per pass, a factor of about 1,500, and it is the same doer either way. Nothing is amplifying here in the exponential sense; it is a full table scan on a timer, which is quite enough.

**Harm starts one to two orders of magnitude below where the arithmetic suggested.** Reasoning from "10,000 entries" implies an attacker sending 33 req/s. Measurably degraded service arrives around 200 entries, which is **under one request per second**, from one source, unauthenticated, against a port that is public by design. At 2 req/s a controller waits 5× longer to be witnessed.

**It is bounded, and it self-heals.** During the run the depth fell from 1,000 back to 823 while the flood was still going, because `TimeoutQNF` was retiring entries faster than the batch added them. A flood that stops is a witness that recovers with no operator action. That is what makes an edge rate limit a real mitigation rather than a partial one — unlike a vector that leaves permanent state.

**It degrades; it does not deny.** The witness answered every request throughout, and `/v1/witness/health` reported `ok` the whole time. That is correct behaviour and is why the disposition stays "hardening" rather than "incident".

## Consequences

**For alerting.** `witness.loop.lag` is the signal, and the measurement gives it a defensible threshold rather than a guess: lag stays at ~0.003 s through 100 entries and passes 0.05 s by 200, which is where a controller first waits noticeably longer. **Alert on `witness.loop.lag` above 0.05 s sustained for a minute**, and use `witness.escrow.depth{store="query_not_found"}` to tell an attack apart from a slow disk — a lag rise with a flat escrow depth is not this.

**Health will not tell you.** `/v1/witness/health` reports `ok` at 0.33 s of lag, ten times the tock. That is deliberate: health answers "is this witness working", and a degraded witness is working. Grading degradation belongs on a threshold an operator sets, not baked into a binary endpoint that would then flap. `tests/test_escrow_load.py` pins this so it does not get "fixed" by accident.

**For the edge.** A per-source rate limit is worth having and is not sufficient. One source at 1 req/s is enough to degrade, so a limit loose enough for legitimate traffic — a witness serves KELs to strangers on demand — still admits it, and a hundred sources at 0.01 req/s each is invisible to any per-source limit. Rate limiting raises the cost of the lazy version; it does not close the vector.

**Upstream.** This is now a measurement rather than a code reading, which is what the standing rule required before saying anything to WebOfTrust. It is still a *hardening* observation and not a defect: `TimeoutQNF` exists precisely to bound this, and it does. If it is ever raised, the useful framing is the slope and the sub-1-req/s onset, not the word "amplification". Nobody has posted anything, and per the standing rule nobody will except Daniel.

## What this does not cover

Single host, single attacker process, one keripy pin, an otherwise idle witness. A witness already serving real traffic starts further along the curve, and a distributed flood was not attempted because the per-source cost is already low enough that distribution adds nothing to the finding. The mitigation was not tested: no measurement was taken with a rate limit in place, because the edge lives in `bakobo/infra`.
