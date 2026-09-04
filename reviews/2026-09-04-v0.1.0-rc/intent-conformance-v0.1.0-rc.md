# Intent & Spec Conformance Review: witness

**Date / Effort / Commit:** 2026-09-04 / deep / `212afdb` (main), run_label `v0.1.0-rc`

## Intent surface audited

Read `this.i` in full (16 nodes) and mapped it against the shipped code surfaces. Nodes exercised:
`q4m7tz` (root goal), `c7v3kp`/`k3p7wr`/`a24p3kbw`/`v27j7uvo` (process split), `n5r2vq`/`vxt7feoi`/
`e3uji3mv`/`vpu373to` (launcher + in-loop telemetry), `w7c4mz`/`f5j3wc` (keripy pin), `t3k6ps`/
`b2y5nr`/`r3v6np`/`h5n2rk`/`wea6qjmk`/`zzbdxa` (v1 control-plane surface), `s6v3qm` (RFC 9421 auth,
deferred), `d4h7kt` (Python), `g3w6px` (CLI), `lypmcw7f`/`vqdcca23`/`lnk24kwp` (image/publish).

Code surfaces enumerated: `src/witness/app.py` (9 routes), `reader.py` (the read views behind them),
`errors.py` (the typed taxonomy), `metrics.py` (OTLP gauges), `inloop.py`/`telemetry.py` (the frozen
hot path), `cli.py`/`config.py` (three subcommands), `supervisor.py`/`runner.py`/`vitals.py`.

Governing specs consulted: `dev/standards/url-design.md`, `http-errors.md`, `error-codes.md`,
`error-handling.md`, `ops.md`; `dev/methodology.md` §3/§5/§9/§10; and the repo's own
`docs/conformance-gap.md` (the evidence `@zzbdxa` was decided against).

Commit-order spot-checks (intent-before-code) run with `git log`/`git show --stat`:
`286ec62`→`b80f87a` (P0 endpoints), `b9c8d36`→`82871d9` (in-loop telemetry), `d767fc2`→
`c0dd9f0`/`c1aa922`/`0624bc1` (image), `e98ae6b`(`@zzbdxa`)→`86848e4` (surface re-cut), and
`7493ade` (`@wea6qjmk`).

## Executive summary

The repo practises the discipline seriously and, for most of its history, exemplarily: strategic
decisions are recorded first, in their own commits, with `why` fields that clear the rebuttal-surface
bar by a wide margin (`a24p3kbw` and `wea6qjmk` are model nodes). The faithfulness gap is concentrated
in the most recent slice of work — the P0+P2 control-plane re-cut and the OTLP metrics — where the
*shipped external contracts outran the tree*. The largest divergence: the entire operator read surface
that shipped in `86848e4` (`/loop`, `/escrow`, `/database`, `/process`, `/version`, `/controller`,
`/controller/{aid}`) is a set of seven new external HTTP contracts with **no `this.i` node recording
them**, even though `@h5n2rk` established for this very service that endpoint response shapes are frozen
contracts a node must govern. The most mechanically-certain defect: `@wea6qjmk` was committed *in the
same commit as* the code it justifies (`7493ade`), which methodology §5 names explicitly as unverifiable.
The most urgent fix before the `v0.1.0-rc` gate (§9.3) is to record the shipped control-plane surface —
routes and response shapes — as intent, because per `url-design.md` a route rename after release costs a
major version.

## Conformance assessment

1. **Code ↔ intent agreement — mixed.** The architecture (process split, read-only LMDB, launcher,
   telemetry, image) matches its nodes faithfully and the code even discharges subtleties the nodes
   promised (`reader._open` proves `@k3p7wr`'s "physically cannot corrupt"; `inloop.py` enforces
   `@e3uji3mv`'s frozen hot path with an AST test). But the v1 HTTP *surface* has drifted past the
   tree: the shipped route set is neither in `this.i` nor matches the only route-level design record
   (`docs/conformance-gap.md` §3 proposed `health`, `identity`, `kel?aid=`, `receipt?aid=`,
   `key-state?aid=`; the code shipped a different set), and `health`'s shape changed after `@h5n2rk`
   froze it. See F1, F3.

2. **Commit order — one violation against an otherwise clean record.** Every intent node except one
   landed in its own commit before its code (`286ec62`, `b9c8d36`, `d767fc2`, `e98ae6b` all verified
   as intent-only commits preceding their code). The exception is `@wea6qjmk`, bundled with `metrics.py`
   and its tests in `7493ade` (F2).

3. **`why` quality — excellent throughout.** Every node names rejected alternatives, accepted
   tradeoffs, and driving constraints, usually with measured evidence. No `why` fails the standard.
   Nothing to flag.

4. **Deviations — none claimed, none obviously owed.** Coverage is gated at `--cov-fail-under=100` and
   `test_smoke.py`/`test_image_smoke.py` are the two deliberately-uncounted suites, explained in
   `AGENTS.md`. No missing `deviation:` node found.

5. **Tensions — none open, none silently re-resolved.** `@a24p3kbw` correctly reopens and *qualifies*
   its parent `@k3p7wr`'s "readonly env open makes cross-process reads safe" claim in the open, rather
   than quietly overriding it — exactly the discipline §5 asks for.

6. **Spec MUSTs — largely honored, with one recorded-but-unreconciled contradiction.** The error
   envelope (RFC 9457 problem+json, `<sorter>.<descriptor>.<disposition>` codes, `Bakobo-Request-Id`)
   conforms to `http-errors.md`/`error-codes.md`, and `@zzbdxa`'s conformance claim holds for the
   envelope. The `/v1/witness/controller/{aid}` route places the AID in a path segment; this is
   **defensible** under `url-design.md`'s `container/concrete-specific-noun` pattern (a controller is a
   singleton resource, one key-state per AID), so it is *not* raised as a violation — but note that the
   repo's own `docs/conformance-gap.md` §3 concluded the opposite ("the AID is a query parameter, not a
   path segment … the standard's call rather than a matter of taste"), and that recorded conclusion was
   reversed without a note. Folded into F1 as evidence.

## Top findings

### F1: The shipped P2 control-plane surface has no intent node — HIGH / LIKELY / Location: `src/witness/app.py:97-105` (routes), `this.i` (no covering node)

- **What the intent says:** `@h5n2rk` records the two P0 endpoints and states "These response shapes
  are frozen external contracts once shipped; changing them needs a new node." That establishes, for
  this service, that HTTP endpoint shapes are §3-trigger external contracts requiring nodes. `@t3k6ps`
  (v1 is read-only audit) and `@b2y5nr` (stateless projection of LMDB) justify *that* there are read
  endpoints; `@wea6qjmk` names escrow/loop/database/process as **OTLP gauges** and explicitly rejects a
  scrape endpoint. No node records the parallel **HTTP GET** surface or its response shapes.
- **What the code does:** `86848e4` shipped seven new routes — `/v1/witness/version`, `/loop`,
  `/escrow`, `/database`, `/process`, `/controller`, `/controller/{aid}` — each with a concrete JSON
  response shape (e.g. `/escrow` → `{"depths": {...}, "total": N}`; `/controller/{aid}` → `{aid,
  sequence_number, said, witnesses, threshold}`). The commit changed no `this.i`.
- **The gap:** these are exactly the "new external contract: API surface, serialization format" the §3
  trigger names, and §10 makes a PR "incomplete" if the nodes are missing. `@zzbdxa` (committed earlier,
  in `e98ae6b`) asserts "every control-plane route conforms" to the standards but predates and does not
  enumerate these routes; it is a conformance claim, not the record of the surface. Corroborating the
  drift: the only route-level design record, `docs/conformance-gap.md` §3 (cited by `@zzbdxa` as its
  evidence, marked "resolved and implemented"), proposes a *different* set (`kel?aid=`, `receipt?aid=`,
  `key-state?aid=`) and reaches the opposite conclusion on AID placement — so the shipped surface
  matches neither the tree nor its own derived doc.
- **Recommendation:** before the gate, add a `decision:` node under `@t3k6ps` (sibling to `@h5n2rk`)
  that enumerates the shipped read routes and freezes their response shapes, with a `why` that states
  why the operator surface is HTTP-GET in addition to the OTLP gauges and reconciles the AID-placement
  reversal against `docs/conformance-gap.md` §3. Do not edit `this.i` in this review.

### F2: `@wea6qjmk` was committed together with the code it justifies — MEDIUM / CONFIRMED / Location: commit `7493ade`, `this.i@wea6qjmk`

- **What the intent process requires:** methodology §5 — "The `this.i` commit that records a decision
  **must be its own commit** and **must appear earlier in `git log`** than the code commit it
  justifies. 'Recorded before code' is not satisfied by an edit in the same commit as the code — the
  commit boundary is the verifiable artifact." §9.3 makes intent-before-code a gate criterion.
- **What the history shows:** `git show --stat 7493ade` — one commit touching `this.i` (+23, the
  `@wea6qjmk` node), `src/witness/metrics.py` (+136), `tests/test_metrics.py` (+169), `cli.py`,
  `pyproject.toml`, `uv.lock`. Intent and its code landed atomically.
- **The gap:** the discipline is rendered unverifiable for this one node — there is no commit boundary
  proving the interview happened before the metrics code existed. This is the single deviation from an
  otherwise clean commit-order record (every other node verified intent-first).
- **Recommendation:** the repo is unreleased (0.0.0), so an interactive rebase splitting `7493ade` into
  an intent-only commit followed by the code commit is cheap and restores the audit trail; alternatively
  record the exception explicitly. Going forward, keep the node commit separate — this is a process slip,
  not a design flaw.

### F3: `/health` changed shape after `@h5n2rk` froze it, with no node — MEDIUM / LIKELY / Location: `src/witness/reader.py:174-207`, `this.i@h5n2rk`

- **What the intent says:** `@h5n2rk` froze `/healthz` as "200 `{"status":"ok"}`, 503 when the witness
  LMDB will not open," and that changing it "needs a new node."
- **What the code does:** the shipped `/v1/witness/health` returns `{"status":"ok","ticks":N|null}` and,
  when the telemetry segment shows the hio loop wedged inside one doer past `_WEDGE_SECONDS`, a **new**
  `{"status":"degraded","reason":…,"ticks":N}` at **HTTP 200** (`c651cd2`). The success body gained a
  `ticks` field and a new `degraded` status value.
- **The gap:** this is a change to an explicitly-frozen external contract. `@zzbdxa` is claimed to be the
  node that unfreezes `@h5n2rk`, but its `why` covers only the *error envelope*, path conformance, and
  the glossary-gated rename — it says nothing about health asserting loop-liveness or emitting `degraded`.
  The loop-wedge health semantics (a genuinely good idea, well-argued in the `app.py`/`reader.py`
  docstrings against `ops.md` §7) are recorded in prose but in no `this.i` node.
- **Recommendation:** fold the health-shape change into the F1 surface node (or a dedicated child of
  `@h5n2rk`), recording the `ticks` field, the `degraded` status, and why a wedged-but-openable witness
  is reported unhealthy — the exact rationale the docstrings already carry.

### F4: The `supervise` CLI subcommand is not recorded in `@g3w6px` — LOW / SPECULATIVE / Location: `src/witness/config.py:84-100`, `this.i@g3w6px`

- **What the intent says:** `@g3w6px` records "one CLI with subcommands — `witness control-plane` (the
  reader, P0) and later `witness run` (the launcher)," enumerating two roles.
- **What the code does:** `cli.py`/`config.py` ship a third subcommand, `witness supervise`, with its
  own external-contract flags (`--essential`, repeatable `--auxiliary`).
- **The gap:** a new CLI subcommand and flags are a §3-trigger external contract ("CLI flags/output
  shape"). `@a24p3kbw` justifies that a supervisor process *exists*, but `@g3w6px` — the node that owns
  the CLI surface — was not extended to record the third role or its flags.
- **Recommendation:** extend `@g3w6px` (or add a child) to record `supervise` as a third subcommand and
  its flag contract; low urgency, but it closes the CLI surface against the same standard the other two
  subcommands meet.

## What's done well

- **Intent-first discipline is real, not decorative.** Four of five intent/code pairs verified as
  intent-only commits landing before their code; the `why` fields are the best-argued I have audited in
  a Bakobo repo, routinely carrying measured evidence (the `a24p3kbw` LMDB-lock experiments; the
  `vqdcca23` wheel-availability measurement; `wea6qjmk`'s reading of `ops.md` §7's own split).
- **Nodes qualify rather than silently re-resolve.** `@a24p3kbw` reopens its parent `@k3p7wr`'s safety
  claim in the open and states the new condition (lock file writable, pids distinct) — the model the
  methodology asks for.
- **Containment is enforced by tests, not comments.** `@e3uji3mv`'s "publishes and never computes" is a
  red AST test, and `reader._open` turns `@k3p7wr`'s corruption-safety claim into a real read-only open
  rather than trusting restraint.
- **Standards conformance for the error surface is genuine.** RFC 9457 problem+json, obstacle-classified
  codes with a trailing disposition token, static titles, and `Bakobo-Request-Id` all match
  `http-errors.md`/`error-codes.md`; the `retryable` flag is derived from the code so the two cannot
  drift.

## Residual unknowns

- **`s6v3qm` (RFC 9421 auth) is deferred, and the whole v1 surface ships unauthenticated.** That matches
  `@t3k6ps`/`@h5n2rk`'s recorded plan, so it is not a conformance defect — but a reviewer should confirm
  the deferral is still the intended posture for a release candidate, since `/controller/{aid}` now
  serves controller key state unauthenticated where `@h5n2rk` originally scoped only low-sensitivity
  `/info`. This is an intent question for the maintainer, not a divergence I can adjudicate.
- **Whether `docs/conformance-gap.md` §3's AID-as-query conclusion was deliberately reversed** or simply
  not carried through to the shipped `/controller/{aid}` route. Recorded here as F1 evidence; the
  maintainer's answer determines whether the doc or the code is the stale one.

### Findings manifest

```yaml
findings:
  - id: CON-F1
    persona: intent-conformance
    title: Shipped P2 control-plane read surface has no this.i node
    severity: HIGH
    confidence: LIKELY
    location: src/witness/app.py:97
    dedupe_key: control-plane-unrecorded
    recommended_disposition: recommend-fix
    rationale: Seven new HTTP GET routes and their response shapes shipped in 86848e4 with no node, though @h5n2rk froze endpoint shapes as node-governed contracts and §10 makes missing nodes a defect.
    revisit_condition: null
    fix_effort: medium
  - id: CON-F2
    persona: intent-conformance
    title: OTLP metrics node @wea6qjmk committed together with its code
    severity: MEDIUM
    confidence: CONFIRMED
    location: this.i@wea6qjmk
    dedupe_key: otlp-metrics-unrecorded
    recommended_disposition: recommend-fix
    rationale: Commit 7493ade bundles the @wea6qjmk node with metrics.py and its tests, which §5 names as making intent-before-code unverifiable; every other node landed intent-first.
    revisit_condition: null
    fix_effort: small
  - id: CON-F3
    persona: intent-conformance
    title: /health shape changed after @h5n2rk froze it, unrecorded
    severity: MEDIUM
    confidence: LIKELY
    location: src/witness/reader.py:196
    dedupe_key: health-divergent
    recommended_disposition: recommend-fix
    rationale: The shipped health endpoint added a ticks field and a new degraded@200 loop-wedge status that @h5n2rk froze and required a node to change; @zzbdxa covers only the error envelope, not health semantics.
    revisit_condition: null
    fix_effort: small
  - id: CON-F4
    persona: intent-conformance
    title: witness supervise subcommand not recorded in @g3w6px
    severity: LOW
    confidence: SPECULATIVE
    location: src/witness/config.py:84
    dedupe_key: cli-unrecorded-supervise
    recommended_disposition: recommend-fix
    rationale: A third CLI subcommand with --essential/--auxiliary flags is a §3-trigger external contract; @g3w6px enumerates only control-plane and run, and was not extended for supervise.
    revisit_condition: null
    fix_effort: small
```

## Disposition

Unattended: findings attached with `recommended_disposition` and rationale; no `this.i` written, no
repo modified. F1 is the one to resolve before the `v0.1.0-rc` gate (§9.3 requires nodes for all
§3-trigger changes since the last gate, and a post-release route rename costs a major version per
`url-design.md`). F2 is cheap to fix on an unreleased repo (rebase-split) or to record as an
acknowledged process slip. F3 folds naturally into F1's surface node. F4 is optional cleanup.
