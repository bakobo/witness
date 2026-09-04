# Developer-Experience Review: bakobo/witness

**Date / Effort / Commit:** 2026-09-04 / medium / 2108341

## Consumer surface examined

This repo exposes two interrelated surfaces:

**CLI** (`witness run`, `witness control-plane`, `witness supervise`) — the mechanism an operator uses to run a witness and its companion control plane. Documented in the README quickstart and `--help` text (via argparse).

**HTTP control-plane API** (9 `GET` endpoints under `/v1/witness/`) — the surface an integrator calls programmatically: health probes, identity, metrics, controller key state. Documented in the README endpoint table plus one error-envelope example.

I played the newcomer role docs-only: read the README and `docs/` without opening source first, then tried to trace the path to a first successful integration call. I then diffed the public surface against what's actually documented, and graded the error envelope and URL shape against `dev/standards/http-errors.md` and `dev/standards/url-design.md`.

## Executive summary

A newcomer to this repo can get from zero to a running witness-plus-control-plane fairly quickly — the README quickstart is concrete and the CLI surface is clean. The main blocker to full integration is that the HTTP response schemas for most endpoints are not documented anywhere (only described in prose), so a developer must read source to write a client or validate a response. A secondary issue is that the error envelope deviates from the declared `http-errors.md` standard in two places (missing `args` and `Content-Language`), and the `/v1/witness/loop` endpoint uses the wrong error class when `--no-telemetry` is in effect, producing a message that sends a developer to the wrong place.

## Assessment

**1. First success.** The README's "From a fresh clone to passing tests" block (`uv sync && uv run pytest`) works. The Docker quickstart is runnable and the two direct-run commands are shown. The `kli init` prerequisite is placed before the `docker run`, which is the right order. One quiet prerequisite (SSH key for `heti`) is stated but not linked to its implication for `uv sync`. Score: good, one small gap.

**2. Surface clarity & consistency.** The URL structure (`/v1/witness/<noun>`) is clean, singular nouns, correct hierarchy. All nine endpoints are `GET`, the one verb in use. Argparse `--help` has per-flag descriptions. The CLI has three distinct subcommands with clear names. Score: good.

**3. Reference completeness & freshness.** The README endpoint table tells a developer *what each endpoint answers* in prose but not *the JSON shape of the answer*. For `/v1/witness/controller/{aid}` "One controller's key state, or `404`" gives no fields. Nine endpoints, zero response schemas documented outside the source. This is the main gap. Score: weak.

**4. Actionable errors.** The error-envelope code and taxonomy are well-grounded: stable codes, retryability in the trailing token, RFC 9457 structure. However the envelope is missing `args` (required by `http-errors.md` for machine-rerenderable errors) and the response is never decorated with `Content-Language: en` (also required). The `/v1/witness/loop` endpoint raises `DbUnavailable` ("I could not open the witness database") when the control plane was started without a telemetry path — wrong class, wrong title, sends a developer hunting the database when the database is fine. Score: two protocol deviations, one actively misleading error.

**5. Examples & the golden path.** The README shows one error-envelope example and one health response excerpt. No success response example for any data endpoint. Score: thin.

**6. Versioning & upgrade.** Version is in `/v1/witness/version`. Breaking-change policy is stated in `url-design.md` rather than in this repo's README. `docs/deploying.md` notes that the upgrade path (migrating LMDB between image versions) has not been exercised. Score: adequate for 0.1.0.

**7. Setup friction & footguns.** The `heti` SSH-URL requirement means `uv sync` fails with an SSH auth error on a machine without a configured GitHub SSH key; the README requirement line says "access to `bakobo/heti`" without saying how that access is proven. `docs/deploying.md` §3 does call out three silent failure modes (Docker chain, split-container LMDB, cgroup sizing) clearly — that's good. The `--no-telemetry` flag's effect on `/v1/witness/loop` is not documented in the endpoint table. Score: one setup gap, one behavioral gap.

## Top findings

### F1: `/v1/witness/loop` returns a wrong error when `--no-telemetry` is used — Severity HIGH / Confidence CONFIRMED / `src/witness/reader.py:212–215`

When `witness control-plane` is started with `--no-telemetry`, calling `GET /v1/witness/loop` returns HTTP 503 with `code: "e.env.witnessdb.unavailable.r"` and `title: "I could not open the witness database."` The detail says "This control plane was not told where the witness publishes its telemetry," which contradicts the title. A developer debugging a loop issue sees "I could not open the witness database," concludes the DB is down, and investigates the DB — which is fine. The real issue is a flag they passed at startup.

The correct error class already exists: `TelemetryUnavailable` (`code: "e.env.telemetry.unavailable.r"`, title: "I could not read the witness's telemetry."). Replacing the `raise DbUnavailable(…)` at `reader.py:213` with `raise TelemetryUnavailable(…)` produces an honest message. The code is still retryable (`.r`), which is slightly wrong for a deliberate `--no-telemetry` configuration (retrying will never help), but at least the title stops blaming the database.

**Recommendation:** Replace `DbUnavailable` with `TelemetryUnavailable` at `reader.py:213`. To make it non-retryable when configured deliberately, either add a new `.f`-coded subclass (`TelemetryNotConfigured`) or document in the endpoint table that `/loop` returns `503` when no telemetry is configured and a client should not retry.

---

### F2: Error envelope missing `args` field — Severity MEDIUM / Confidence CONFIRMED / `src/witness/errors.py:47–59`

`http-errors.md` requires every error envelope to carry an `args` list — "the situational values, positional, for clients that render their own text." The `problem()` method on `WitnessError` never emits `args`, and the test at `test_app.py:103–110` codifies the deviation by asserting the exact response without it.

The gap is concretely observable for `ControllerUnknown`, whose detail interpolates the AID: "This witness holds no key state for {aid}; it may not witness that controller." A standards-compliant client that wants to re-render the message needs `args: [aid]` to do so without parsing prose. Currently it cannot.

**Recommendation:** Add `args` support to `WitnessError` — a class attribute `args_from_message: tuple = ()`, overridden by subclasses that have situational values, and emitted as `document["args"] = self.args_values` in `problem()`. Update the test to include `"args": []` (or the specific values for `ControllerUnknown`).

---

### F3: Error responses never set `Content-Language: en` — Severity MEDIUM / Confidence CONFIRMED / `src/witness/app.py:73–80`

`http-errors.md` states: "problem responses declare `Content-Language: en`." The `Endpoint.on_get` error path sets `Content-Type: application/problem+json` and the body but never sets `Content-Language`. A client validating the response against the standard cannot confirm the language, and a future localization effort would have no existing header to evolve.

**Recommendation:** Add `resp.set_header("Content-Language", "en")` in `Endpoint.on_get` after setting `resp.content_type`, and add a test asserting it on any error response.

---

### F4: Response schemas undocumented for most endpoints — Severity MEDIUM / Confidence CONFIRMED / `README.md:60–71`

The endpoint table describes what each path *answers* in prose ("One controller's key state, or `404`") but gives no JSON shape. A developer writing a client for `/v1/witness/controller/{aid}` must open `src/witness/reader.py:281–296` to discover the fields (`aid`, `sequence_number`, `said`, `witnesses`, `threshold`). The same is true for `/v1/witness/loop` (fields documented only in the source), `/v1/witness/escrow` (depth map structure), and `/v1/witness/database`.

`docs/foreseen-work.md` records this as deferred: "Re-evaluate FastAPI/OpenAPI — tick `6cy6` — Due at the web-console or mutation work." The deferral is recorded and reasonable; the gap is still a real barrier to first integration.

**Recommendation:** The tick already records this. As a lower-cost interim, add one response-shape example per endpoint to the README table — the same format as the existing error-envelope example. This is a one-PR patch that doesn't wait for the OpenAPI migration.

---

### F5: `heti` SSH-key prerequisite is not stated in the quickstart — Severity LOW / Confidence CONFIRMED / `README.md:17–18`

The Requirements section says "Access to `bakobo/heti`, which is a private dependency." The `pyproject.toml` pins `heti` via `git+ssh://…` — so `uv sync` authenticates over SSH. A developer with GitHub access but no configured SSH key sees:

```
error: Failed to build `heti @ git+ssh://git@github.com/bakobo/heti@cd836…`
Host key verification failed.
```

The README doesn't say "you need a GitHub SSH key" or link to how to add one.

**Recommendation:** Add one sentence to the Requirements section: "The `heti` dependency fetches over SSH; your environment must have a GitHub SSH key that has access to `bakobo/heti`." The pyproject.toml comment already says this internally ("SSH because heti is private and every Bakobo developer already has SSH keys") — surface it for the human who reads the README first.

---

## What's done well

The URL design is clean and standard-conformant: `/v1/witness/<singular-noun>`, general-to-specific, all GETs, no RPC verbs in path. The error taxonomy is well-founded — stable codes, retryability in the trailing token, RFC 9457 envelope with `type` as a stable dereferencing URL. The `--no-telemetry` mode is documented as a flag (even though its effect on `/loop` is not). `docs/deploying.md` is exceptionally candid about the three failure modes that are silent when wrong (Docker chain, split-container LMDB, cgroup sizing), which is exactly the cost-shifting the DX lens looks for. The CLI `--help` text has per-flag descriptions and sensible defaults. The 405 / Allow-header behavior on wrong-verb requests is tested explicitly.

## Residual unknowns

The review examined the documented and implemented surface as of commit 2108341. Two surfaces are deferred (authentication via RFC 9421, mutation operations) and were not graded — the authentication gap is explicitly noted as out-of-scope for this release and doesn't affect read-path consumers today. The image-smoke test (`test_image_smoke.py`) was not run (it requires a live Docker image); the runtime behavior of the control plane was inferred from source and unit tests only.

---

## Findings manifest

```yaml
findings:
  - id: DX-F1
    persona: dev-experience
    title: /v1/witness/loop raises DbUnavailable ("witness database") for no-telemetry config
    severity: HIGH
    confidence: CONFIRMED
    location: src/witness/reader.py:212
    dedupe_key: loop-divergent
    recommended_disposition: recommend-fix
    rationale: developer debugging loop issues sees "I could not open the witness database" when DB is fine; sends investigation to wrong place
    revisit_condition: null
    fix_effort: small
  - id: DX-F2
    persona: dev-experience
    title: Error envelope missing args field required by http-errors.md
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/witness/errors.py:47
    dedupe_key: error-envelope-missing
    recommended_disposition: recommend-fix
    rationale: standards-compliant clients cannot re-render error messages from machine-readable data; ControllerUnknown embeds AID in prose with no args
    revisit_condition: null
    fix_effort: small
  - id: DX-F3
    persona: dev-experience
    title: Error responses omit Content-Language header required by http-errors.md
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/witness/app.py:73
    dedupe_key: response-header-missing
    recommended_disposition: recommend-fix
    rationale: http-errors.md requires Content-Language en on every problem response; omitted across all endpoints
    revisit_condition: null
    fix_effort: small
  - id: DX-F4
    persona: dev-experience
    title: Response schemas undocumented for most control-plane endpoints
    severity: MEDIUM
    confidence: CONFIRMED
    location: README.md:60
    dedupe_key: endpoint-undocumented
    recommended_disposition: recommend-defer
    rationale: developer must read source to know response field names for /loop, /escrow, /database, /controller; deferred via tick 6cy6 pending OpenAPI evaluation
    revisit_condition: tick 6cy6 resolved or a web-console surface ships
    fix_effort: medium
  - id: DX-F5
    persona: dev-experience
    title: SSH key prerequisite for heti not stated in quickstart Requirements
    severity: LOW
    confidence: CONFIRMED
    location: README.md:17
    dedupe_key: heti-presumed
    recommended_disposition: recommend-fix
    rationale: uv sync fails with SSH host-key error on a machine without GitHub SSH configured; one sentence in Requirements would surface this
    revisit_condition: null
    fix_effort: small
```
