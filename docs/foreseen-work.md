# Foreseen work — the whole surface

> **Partly built since, 2026-09-04.** The launcher, in-loop telemetry, the container image and the
> read-only control-plane surface have all landed; see `this.i` and `docs/deploying.md` for what
> actually exists. This map has not been rewritten around them, so read it for the shape of the
> surface rather than for current status.

*Written 2026-08-02 to answer a question that could not be answered without it: what work has this
repo actually foreseen? Every entry is traced to the `this.i` node or tick that implies it, and
separated by how firmly it is established. No sequencing and no phase labels — the point is to see
the surface before drawing lines through it. `this.i` remains the source of truth; this document is
derived from it and carries no authority of its own.*

Three tiers, and the difference matters:

- **Recorded** — a `this.i` node or tick says this, in words. Cited.
- **Implied** — follows from something recorded, but no one has written it down. My inference.
- **Speculative** — mine, with nothing in the record behind it. Included so the map has edges, not
  because it belongs.

---

## Already built

| Capability | Where | Source |
|---|---|---|
| Liveness probe | `GET /healthz` | `h5n2rk` |
| Witness identity | `GET /info` — aid, alias, keripy version, db path | `h5n2rk` |
| Read-only LMDB open, per request | `src/witness/reader.py` | `k3p7wr` |
| Phantom-env guard on a missing DB | `reader._resolve_existing_db` | tick `5s3e` |
| Typed error taxonomy with retryable flag | `src/witness/errors.py` | Bakobo error standard |
| One CLI, subcommand per role | `witness control-plane` | `g3w6px` |

Both endpoint shapes are frozen external contracts (`h5n2rk`).

---

## Recorded

### Observing the witness

`b2y5nr` names what a stateless projection of keripy's LMDB can serve, and this is the list:

| Capability | Source | keripy surface it rides |
|---|---|---|
| Enumerate the AIDs this witness holds events for | `b2y5nr` | `db.fels` (**not** `db.habs` — see below) |
| Current key state per AID | `b2y5nr` | `db.states` |
| Retrieve an AID's KEL | `b2y5nr`, `w7c4mz` | `db.clonePreIter` |
| Retrieve receipts | `b2y5nr` | witness receipt couples |
| First-seen timeline — when this witness first saw each event | `b2y5nr`, `w7c4mz` | `db.dtss`, `db.fels` |

Established by the contract tests in `tests/test_keripy_contract.py`, and correcting an earlier
guess in this document: **`db.habs` holds only the witness's own identity**, not a record of whom it
witnesses. The witnessed controller's prefix does not appear there at all. So the index endpoint
cannot be built on `habs`; enumerating witnessed AIDs means iterating `db.fels`, which puts a
*semi-internal* accessor on the critical path of the very first data endpoint rather than a public one.

`w7c4mz` flags that `db.fels` and `db.clonePreIter` are *semi-internal* keripy accessors, and commits
to "a commit pin plus contract tests" as the guard against upstream drift. Note that today's reader
touches none of them — it reads `habs` only — so that commitment attaches to work not yet done rather
than to a gap in what exists. It becomes due with the first endpoint that reads a KEL.

### Controlling access

| Capability | Source |
|---|---|
| Authenticate each request as RFC 9421 HTTP Message Signatures, via `heti.l0.verify_request` | `s6v3qm` |
| Authorize against an operator-AID allowlist | `s6v3qm` |

`s6v3qm` also fixes the credential shape: the caller proves a **non-transferable** AID. heti's L0
rejects transferable AIDs outright, so an operator's credential is a bare Ed25519 key, not their
organizational KERI identity.

### Running the witness

| Capability | Source |
|---|---|
| A launcher that imports keripy as a library and starts the stock `setupWitness` doers unchanged | `n5r2vq` |
| A `witness run` CLI subcommand for it | `g3w6px` |
| One control-agent doer added to the running loop | `n5r2vq` |
| An IPC control socket exposed by that doer | `n5r2vq`, `k3p7wr` |

`n5r2vq` is explicit that the launcher exists *to create the seam* — the reason not to use stock
`kli witness start` is that it leaves nowhere to attach control later. `t3k6ps` adds that the seam is
"designed now but ships inert."

### Governing the witness

The root goal `q4m7tz` is "observable **and governable**," so mutation is in scope from the top, not
an extension. `t3k6ps` names the three deferred operations:

| Capability | Source |
|---|---|
| Pause | `t3k6ps` |
| Ban | `t3k6ps` |
| Config reload | `t3k6ps` |

Each of these has to cross the IPC boundary rather than touch LMDB, per `k3p7wr`.

### Beyond a stateless projection

`b2y5nr` chose *not* to keep our own datastore, and named the concrete features that would reopen it:

| Capability | Source | Why it breaks statelessness |
|---|---|---|
| Metrics over time | `b2y5nr` | LMDB holds current state, not history of our observations |
| Changes-since-root sync index | `b2y5nr` | An index we maintain, not one keripy keeps |
| Fleet sync across witnesses | tick `5gsx` | Coordination state spanning hosts |
| Independent, tamper-evident audit store | `b2y5nr` | Explicitly called out as absent |
| Web console | `r3v6np`, tick `6cy6` | A UI, plus the mutating POST surface it drives |

### Engineering obligations already committed to

| Obligation | Source | State |
|---|---|---|
| Contract tests pinning the semi-internal keripy accessors | `w7c4mz` | Due with the first KEL read |
| Reuse the phantom-env guard in the runner and any future DB reader | tick `5s3e` | Due with the runner |
| Adversarial multi-persona review by `bakobo-review-panel` | tick `45hj` | Due at the end of the first authed slice |
| Re-evaluate FastAPI/OpenAPI | tick `6cy6` | Due at the web-console or mutation work |
| Evaluate hio Boss/Crew multidoing as the IPC mechanism, rather than a bespoke socket | tick `5gsx` | Due with the control channel |

---

## Implied but never recorded

Each of these follows from something above, and nothing in the repo settles it. They are the
decisions that will get made silently if nobody names them.

- **Where the operator allowlist comes from, and how it changes.** `s6v3qm` says there is one. It
  does not say whether it is a file, a flag, or a table, nor whether changing it restarts the
  process.
- **Replay defense.** heti's L0 deliberately performs no timestamp-freshness check — `dialect.py:12`
  says so, and heti's own `this.i` makes freshness an L1 concern. A captured signed GET therefore
  replays indefinitely unless witness bounds it. Nothing in witness's record acknowledges this.
- **Operator key rotation.** A non-transferable AID is a bare key with no key history by
  construction. Rotating an operator's credential means editing the allowlist; there is no
  cryptographic rotation path. `s6v3qm` doesn't mention it.
- **Pagination and response bounds.** A KEL is unbounded. No recorded position on paging, streaming,
  or a maximum response size.
- **Whether one control plane serves one witness or many.** Everything recorded assumes one, but
  nothing states it, and fleet sync (tick `5gsx`) points the other way.
- **Observability of the control plane itself** — its own logs, metrics, and failure reporting, as
  distinct from what it reports about the witness.
- **TLS and network placement.** `h5n2rk` defaults the bind to `127.0.0.1` without saying whether
  that is the deployment model or a safe default.

## Speculative

Mine, with nothing behind them. Listed so the edges of the map are visible.

- Alerting when the witness observes duplicity, rather than only answering when asked.
- Backup and restore of the witness database.
- Rate limiting, which only matters under a network-facing posture.
- Multi-tenancy, if one operator layer ever serves witnesses belonging to different controllers.

---

## What this inventory does not settle

Sequencing. Nothing above says what ships when, and the record gives almost no help: `this.i` and the
ticks name P0, P1 and P4 and never define P2 or P3, so there is no recorded ladder to slot this into.
Drawing those lines is the next conversation, and it is a separate one from this list.
