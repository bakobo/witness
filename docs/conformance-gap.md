# Conformance gap: the shipped surface against the org standards

*Written 2026-08-06. **Nothing here is decided.** It exists so that one question — whether
`h5n2rk`'s freeze on `/healthz` and `/info` survives contact with org standards written after
them — can be adjudicated against evidence rather than against a summary of it. Until that is
settled in `this.i`, treat every proposal below as a proposal.*

The four standards in force, all in `~/code/bakobo/dev/standards/`: `url-design.md` (namespace),
`http-errors.md` (the HTTP binding), `error-codes.md` (identity and taxonomy), `error-handling.md`
(message quality). All four claim every Bakobo HTTP surface.

---

## 1. What is shipped today

| Route | Success | Failure |
|---|---|---|
| `GET /healthz` | `200 {"status": "ok"}` | `503 {"status": "unavailable", "error": {code, message, retryable}}` |
| `GET /info` | `200 {aid, alias, keripy_version, db_path}` | `503 {code, message, retryable}` |

Codes in `src/witness/errors.py`: `witness.error`, `witness.db.unavailable`,
`witness.identity.unavailable`, `witness.config.invalid`.

## 2. The gaps

| # | Gap | Standard |
|---|---|---|
| 1 | No `/v<major>/<component>/` prefix. Both routes sit at the root. | `url-design.md:72` |
| 2 | `witness` is not a glossary lemma, so the component segment cannot be minted yet. The glossary has 41 terms including `watcher`, `heti`, `imbu`, `soka` — not `witness`. | `url-design.md:80`, `terminology.md` |
| 3 | `healthz` is an invented abbreviation. | `url-design.md:139` |
| 4 | Error bodies are plain JSON, not `application/problem+json`. | `http-errors.md:102` |
| 5 | Bodies carry `message`/`retryable`; the envelope wants `code`, `type`, `title`, `detail`, `args`, `instance`, `request_id`. | `http-errors.md:107` |
| 6 | No `Content-Language: en`, no `Bakobo-Request-Id` header. | `http-errors.md:115`, `:179` |
| 7 | Every code names the component. "No component name, no service name, no library name in a code" is categorical. | `error-codes.md:210` |
| 8 | Codes carry no `f`/`r` disposition token; retryability is a separate boolean field. | `error-codes.md:66` |
| 9 | `witness.error` is a bare base with no sub-descriptor. | `error-codes.md:230` |
| 10 | The two routes use *different* failure shapes — `/healthz` nests under `"error"`, `/info` does not. Inconsistent siblings. | `error-handling.md:89` (rubric #10) |

Gap 10 is the one I would flag even if no standard existed. Two endpoints in the same 130-line
service disagree about where the error lives.

## 3. Proposed routes

Derived, not invented. `url-design.md:121` says a value belongs in the path only if varying it
changes the **shape** of what is returned, and in the query string if it only changes **which
instance** you get.

| Proposed | Replaces / adds |
|---|---|
| `GET /v1/witness/health` | `/healthz` |
| `GET /v1/witness/identity` | `/info` |
| `GET /v1/witness/kel?aid={aid}` | new |
| `GET /v1/witness/receipt?aid={aid}` | new |
| `GET /v1/witness/key-state?aid={aid}` | new |

Note what the insight test does to the AID. Varying it does not change the shape of a KEL
response — every AID's KEL is the same shape — so the AID is a **query parameter, not a path
segment**. The obvious-looking `/aid/{aid}/kel` nesting is therefore wrong, and it is the
standard's call rather than a matter of taste.

Prerequisite for all of it: `witness` must be minted in `bakobo/glossary`. That is an authority
act and not mine to perform.

## 4. Proposed error codes

Classified by **obstacle**, per the closed first-descriptor set at `error-codes.md:110`.

| Today | Proposed | Descriptor reasoning | Status |
|---|---|---|---|
| `witness.db.unavailable` | `e.env.witness-db.r` | `env` is "a system we depend on that did not deliver." The witness process and its LMDB are exactly that. Retryable — it may not be running yet. | 503 |
| `witness.identity.unavailable` | `e.env.witness-uninitialized.r` | The database opened but holds no witness hab. **Boundary call, flagged rather than settled:** `e.state.pending.r` is arguable, since the obstacle is the condition of the target — but that maps to 409, which reads wrong on a liveness-adjacent read. `error-codes.md:222` says consult before minting. | 503 |
| `witness.config.invalid` (missing arg) | `e.input.missing.f` | Splits in two. Today one code covers both cases. | 400 / CLI |
| `witness.config.invalid` (bad port) | `e.input.range.f` | The offending value and the valid range go in `args`. | 400 / CLI |
| `witness.error` | *not a code* | Becomes the base exception class carrying an `ErrorCode`, mirroring `HetiError`. A bare descriptor is never a code. | — |

Reuse rather than rebuild: `ErrorCode`, `HetiError` and `matches` already exist in
`~/code/bakobo/heti/src/heti/errors.py`, and witness now depends on heti. The standard describes
these as living in a "shared Bakobo error package" (`error-codes.md:94`) that does not exist as its
own repo — so importing from heti is the available option, and worth its own decision if
`bakobo/errors` is ever extracted.

Register on the heti pattern, literals at module scope, static titles, named args:

```python
DB_UNAVAILABLE = ErrorCode(
    "e.env.witness-db.r",
    "I could not open the witness database.",
    detail="No witness database was readable at {path}; the witness may not be running yet.",
    args=("path",),
    hint="Confirm the witness process is running and that --name and --base match its keystore.",
)
```

## 5. Draft intent nodes — proposals, not written

Neither has been added to `this.i`. Writing one is the most reserved act in the methodology.

**Draft A — if the shipped surface is re-cut.** A child of `t3k6ps`, superseding `h5n2rk`:

> *The v1 HTTP surface conforms to the org standards, including the two shipped routes = decision.*
> why: `h5n2rk` froze `/healthz` and `/info` before `url-design.md`, `http-errors.md` and
> `error-codes.md` existed, and all three claim every Bakobo HTTP surface. Chose to re-cut rather
> than grandfather because witness is private, at 0.0.0, with no external consumer, so the freeze
> protects nobody today and protects more the longer it stands — and because a split error envelope
> across two endpoints of one service is the inconsistency `error-handling.md` rubric #10 names.
> Rejected conforming only new routes, which buys a permanent exception to a standard that claims
> universality, in exchange for avoiding a rename nobody would notice. Accepted tradeoff: the
> component segment requires minting `witness` in the glossary first.

**Draft B — if the freeze holds.** A deviation node, since it is a recorded exception to a
standard rather than a decision within one, and `error-handling.md` and `http-errors.md` are
enforced by review lenses that will otherwise re-raise it every time.

## 6. What this does not settle

Whether responses carry evidence or projections, and what the deployment posture is. Neither is
touched by these standards — they constrain the envelope and the namespace, not the payload.
`bakobo/schema` owns payload schemas per `url-design.md:189`, which is itself a thread nobody has
pulled for witness.
