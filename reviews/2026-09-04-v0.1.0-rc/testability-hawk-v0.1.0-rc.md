# Testability Review: bakobo/witness
**Date / Effort / Commit:** 2026-09-04 / medium / 2108341 (main, clean)

---

## Test surface examined

**Test command:** `uv run pytest` — `--cov=witness --cov-branch --cov-report=term-missing --cov-fail-under=100`

**CI enforcement:** Yes. The `Run tests (100% branch coverage gate)` step in `.github/workflows/ci.yml` runs exactly that command. Any missed branch fails the build. No xfail/skip abuse was found in the unit suite.

**Units mapped:**

| Module | Lines | Branches | Notes |
|---|---|---|---|
| `app.py` | 44 | 2 | Endpoint error envelope, RequestCounter, RequestIdMiddleware |
| `reader.py` | 118 | 36 | WitnessReader — all views, including identity discrimination and wedge detection |
| `inloop.py` | 80 | 10 | `timed()` wrapper, `TimedDoist`, `TelemetryDoer` |
| `telemetry.py` | 125 | 18 | SegmentWriter / SegmentReader, seqlock round-trips |
| `runner.py` | 37 | 4 | WitnessRunner assembly, open_habery branching |
| `metrics.py` | 58 | 14 | Observable gauge callbacks, OTLP wiring |
| `vitals.py` | 37 | 10 | /proc parse, runner-process detection |
| `supervisor.py` | 69 | 16 | Asymmetric restart policy |
| `config.py` | 80 | 10 | Subcommand dispatch, port validation |
| `errors.py` | 45 | 4 | Error taxonomy, RFC 9457 envelope |
| `server.py` | 4 | 0 | waitress shim |
| `cli.py` | 18 | 4 | main() dispatch |
| **TOTAL** | **718** | **128** | **100% statements and branches** |

**Verify/auth paths read carefully:**

- `reader.health()` — DB open, wedge detection (`_WEDGE_SECONDS`), telemetry error handling
- `reader.identity()` — non-transferable hab selection (`_is_witness_hab`, `_select_witness_hab`)
- `inloop.timed()` — StopIteration forwarding, GeneratorExit, exception propagation, enter-slot ordering
- `telemetry.SegmentReader.read()` — seqlock retry loop, torn-read detection
- `metrics.build_callbacks()` — `guarded()` closure, error-to-gap conversion per callback
- `test_heti_contract.py` — heti API surface, transferable-AID rejection

**Two deliberate out-of-scope smoke tests:**

- `tests/test_smoke.py` — subprocess end-to-end, excluded from coverage counting
- `tests/test_image_smoke.py` — Docker image oracle, skipped unless `WITNESS_IMAGE` is set

---

## Executive summary

The test suite is honest: 100% branch coverage is reached without inflation, and the riskiest structural properties (seqlock correctness, generator-wrapper transparency, hot-path freeze, read-only LMDB isolation, wedge detection) are tested with real assertions rather than shallow smoke. The most significant gap is in metric gauge correctness: `test_metrics.py` exercises metadata and the "unreadable witness yields a gap" property for every gauge, but never asserts the numeric VALUES the callbacks return, so a wrong key name or arithmetic error would be invisible. A secondary gap is that no test on the HTTP surface exercises an auth-rejection path; auth is deliberately deferred to P1 per `this.i` @t3k6ps/@s6v3qm, but when it lands it needs rejection tests that the current test structure does not template. Both `# pragma: no cover` uses are defensible guards, not significant holes, though they each hide an untested branch from the 100% gate.

---

## Assessment

### 1. Untestable design
**Verdict: Largely clean.** Every collaborator is injected: `WitnessRunner` takes `open_habery`, `build_doers`, `create_segment`, and `doist_factory`; `TimedDoist` takes `sink`; `TelemetryDoer` takes `monotonic`; `Supervisor` takes `spawn` and `sleep`; `vitals` functions take `proc=`. No hidden `new` or module-level state constructors block unit testing. The one structural question is `TimedDoist.enter`'s `sink is None` guard, which is pragmatically excluded from coverage — a cosmetic defect, not a design blockage.

### 2. Hollow assertions
**Verdict: One meaningful gap.** The inloop, telemetry, reader, errors, config, supervisor, and app tests assert on actual values and shapes. The metrics tests are structurally sound for metadata and gap behavior but do not assert the numeric values most callbacks return. See F1.

### 3. Missing sad paths
**Verdict: Well covered.** The DB-unavailable, DB-corrupt, DB-missing, LMDB-readonly-silently-writable, teardown-during-scan, process-vanishes-mid-read, telemetry-torn-write, telemetry-incompatible-magic, telemetry-future-version, controller-absent, foreign-keystore, and witness-not-incepted paths are all explicitly tested. The one gap is HTTP-surface coverage for error paths of the `loop`, `escrow`, `database`, `process`, and `controllers` views — the mechanism is uniform (`Endpoint.on_get` catches `WitnessError` for every view) and is tested for health/identity/controller, so this is low-risk duplication, not a structural omission.

### 4. Authority path
**Verdict: Deferred by recorded decision; scaffolding present but incomplete.** `this.i` @s6v3qm commits to RFC 9421 auth via heti; @t3k6ps/@h5n2rk record its deferral to P1. No production code calls `heti.ephemeral.verify_request` today. `test_heti_contract.py` proves heti rejects transferable AIDs and still exposes the right API surface, but no test on the control-plane HTTP surface proves that an unauthenticated or forged request is rejected. When P1 auth lands, it needs rejection tests that the current structure does not template. See F2.

### 5. Flakiness
**Verdict: Unit suite is clean; smoke tests carry a TOCTOU port race.** All unit tests use injected clocks, synthetic /proc trees, and tmp_path fixtures. No wall-clock coupling, no shared mutable state across tests, no iteration-order sensitivity. The `_free_port()` function in both smoke test files releases the ephemeral port before the subprocess or Docker container binds it; a concurrent process can steal it in the race window. See F3.

### 6. Test-shape coupling and upkeep
**Verdict: Good, with two intentional exceptions.** `test_inloop.py::test_the_hot_path_stays_frozen` and `test_every_expected_hot_path_function_is_still_marked` are deliberately coupled to function names in `inloop.py` and `telemetry.py` — that coupling IS the enforcement mechanism, and the tests document this. `test_keripy_contract.py` is similarly coupled to keripy internals by design. All other tests use behavior-level assertions. The conftest.py fixtures use hardcoded branes for determinism, not to couple to key material.

### 7. CI actually runs them
**Verdict: Yes, with no suppressed suites.** The CI job runs `uv run pytest` with no `--ignore`, no `--ignore-glob`, no `-k` exclusion. The smoke tests are excluded from the `addopts` gate only because they are architecturally separate (subprocess / Docker) and AGENTS.md documents this explicitly. The 100% branch gate is enforced on every push to main and every PR.

---

## Top findings

### F1: Metric gauge values are not asserted — wrong key or arithmetic would be invisible — MEDIUM / CONFIRMED / `metrics.py:61-91`

`test_metrics.py` proves that every gauge has a name, unit, and description; that `witness.escrow.depth` reports the right store-keyed attributes; that `witness.loop.doer.max_seconds` reports the right doer-keyed values; and that a failing reader yields `[]` rather than raising. It does not assert the numeric VALUE that most callbacks return. Seven of nine gauges go unchecked:

- `witness.loop.lag` — reads `reader.loop()["loop_lag"]`; stub returns 0.5; no test asserts the observation equals 0.5
- `witness.loop.ticks` — reads `reader.loop()["ticks"]`; stub returns 5; no value assertion
- `witness.database.used_fraction` — reads `reader.database()["used_fraction"]`; stub returns 0.25; no value assertion
- `witness.process.resident_bytes` — reads `reader.process()["resident_bytes"]`; stub returns 1024; no value assertion
- `witness.process.cpu_seconds` — reads `reader.process()["cpu_seconds"]`; stub returns 3.5; no value assertion
- `witness.up` — `0` vs `1` is asserted (F1-safe)
- `witness.controlplane.requests` — value asserted (F1-safe)

If any callback read the wrong dict key (e.g. `loop()["lag"]` instead of `loop()["loop_lag"]`, which would be a `KeyError` caught by `guarded()` and silently returned as `[]`), the test would still pass — but the gauge would disappear from every scrape rather than reporting lag. For an ops-critical signal this is silent misbehavior.

**Fix:** For each of the seven gauges, add one test that calls `collect(StubReader(), gauge_name)` and asserts the observation value matches the stub's return.

---

### F2: No HTTP-surface test for auth rejection — MEDIUM / LIKELY / `tests/test_app.py` (absent), `tests/test_heti_contract.py`

`this.i` @s6v3qm commits to RFC 9421 auth on every control-plane request; @t3k6ps/@h5n2rk record deferral to P1, when the first endpoint serving witnessed KEL/receipt data ships. Currently, all nine endpoints are served without any auth middleware or per-request credential check. `test_heti_contract.py::test_verify_rejects_a_transferable_aid_rather_than_best_effort_checking_it` proves that `heti.ephemeral.verify_request` rejects a transferable AID, but that function is not called from any production code path. When auth lands, it needs tests proving:

1. A request with no `signature-input` / `signature` headers is rejected with an appropriate status (likely 401).
2. A request signed by an AID not on the operator allowlist is rejected (likely 403).
3. A request with a syntactically invalid signature is rejected (not silently passed).
4. A request signed by a valid non-transferable AID on the allowlist succeeds.

None of these test shapes exist in `test_app.py` or anywhere else. The heti contract tests prove the library can do the job; they do not prove the middleware wires it correctly.

**Recommendation:** Defer per recorded decision, but open a tick capturing the four test cases that P1 auth must bring. `CON` should verify that @s6v3qm's deferral has a matching tension: node in `this.i` before P1 ships.

---

### F3: Smoke test `_free_port()` releases the port before the subprocess can bind it — LOW / CONFIRMED / `tests/test_smoke.py:20`, `tests/test_image_smoke.py:41`

Both smoke files share the same `_free_port()` implementation: bind an ephemeral port to learn its number, then close the socket, then hand the number to a subprocess. Another process can bind the same port in the race window between the `with` block's close and the subprocess starting. On a loaded CI runner this is a plausible but rare failure — the test would report "the control plane never came up" rather than "port stolen", making the diagnosis non-obvious.

**Fix (small):** Use `SO_REUSEPORT` (Linux) or keep the socket open until the subprocess has confirmed binding — or simply accept a retry loop on `ConnectionRefusedError` as the smoke tests already do, since the failure mode is the same either way. The simplest fix is `sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)` before binding, then close only after the subprocess starts.

---

### F4: Two `# pragma: no cover` branches hidden from the 100% gate — LOW / CONFIRMED / `inloop.py:110`, `vitals.py:83`

**`inloop.py:110`** — `TimedDoist.enter` returns early if `self.sink is None`. Production always passes a `SegmentWriter`; unit tests always pass a `RecordingSink`. The comment says "a Doist built without telemetry". If something in this branch were wrong — the deeds returned in wrong order, or the return value dropped — no test would catch it. Risk is low (the branch is `return deeds`, two words), but the pragma lets it hide from the gate.

**`vitals.py:83`** — `_kb(value)` returns `None` when `value is None` (VmRSS absent from /proc/pid/status). "Every Linux kernel reports VmRSS for a live process." The comment is correct; the pragma is reasonable. Risk: if this `None` propagated into the metrics callback, the OTLP exporter would receive a null value with no test having exercised that path.

**Fix (small for both):** Remove each pragma and add a one-line test that constructs the object without a sink / calls `_kb(None)` and asserts the return. Both tests are trivial; their absence is the only cost.

---

### F5: `vitals.runner_vitals()` does not assert `resident_bytes` in its unit test — LOW / CONFIRMED / `tests/test_vitals.py:83`

`runner_vitals()` returns both `resident_bytes` (computed from `/proc/<pid>/stat` field 21 × page size) and `vm_rss_bytes` (parsed from `/proc/<pid>/status` VmRSS). The metric callback `process_resident` reads `resident_bytes`. `test_vitals_report_cpu_memory_threads_and_descriptors` asserts on `vm_rss_bytes` (64 × 1024) but not on `resident_bytes`. The make_proc fixture sets `rss=4096` by default, so `resident_bytes` would be `4096` — but this is never checked. If field 21's index or the `_PAGE_SIZE` arithmetic were wrong, the metric would silently report a wrong value and no test would fail.

**Fix (small):** Add `assert reported["resident_bytes"] == 4096` (or `== rss`) to the existing test, or parametrize with different `rss` values.

---

## What's done well

**Generator-wrapper transparency is verified, not assumed.** `test_inloop.py` drives the `timed()` wrapper through send, StopIteration, GeneratorExit, and exception paths and asserts each against the bare generator equivalent. This is exactly the right test for code that sits between hio and every keripy doer.

**The seqlock is tested against real write races.** `test_telemetry.py` manufactures a mid-write read (odd sequence) and a write that lands inside a snapshot, proving both retry paths. These are not trivially covered by happy-path tests.

**The hot-path freeze is structural.** `test_the_hot_path_stays_frozen` parses the AST at test time and fails on any call outside the allowlist. The companion `test_every_expected_hot_path_function_is_still_marked` ensures @hot_path cannot be quietly removed to escape the check. Two tests that are hard to game.

**LMDB read-only isolation is a property test, not an assumption.** `test_open_yields_a_genuinely_readonly_environment` calls `rdb.env.begin(write=True)` and asserts it raises. This tests what keripy actually hands back, not what the flag was supposed to mean — a distinction that matters because the flag is silently discarded by `Baser(reopen=True, readonly=True)`.

**The wedge detector is tested against a live-looking false alarm.** `test_a_doer_merely_executing_is_not_a_wedge` exercises the exact failure mode an earlier version exhibited against a real witness: sampling catches a doer mid-execution and the detector called it stuck. The test fixes the threshold semantics and prevents regression.

**Contract tests guard the keripy/heti dependency pins.** The contract suites (keripy accessors, heti API surface) fail immediately on an upstream bump that moves a symbol, so drift is loud rather than silent. The `_LOCK` cross-check in heti_contract ensures both dependencies resolve to the same keripy commit, and the canary for the `EphemeralVerdict` rename is a particularly well-chosen tripwire.

**Supervision policy is tested without spawning anything.** `FakeProc` / `Spawner` let the asymmetric restart policy be exercised at pure Python speed. The test that prevents the supervisor's own command line from being mistaken for the runner's is a real regression guard for a specific footgun.

---

## Residual unknowns

- The `_WEDGE_SECONDS = 5.0` threshold is reasoned from "~160 tocks at 0.03125s" but there is no test that varies the threshold or checks boundary behaviour (4.999s → ok, 5.001s → degraded). The existing tests use 60s held time, well above the threshold. A test at exactly the boundary would be more precise.
- `test_image_smoke.py::test_the_running_witness_publishes_telemetry` sleeps 2 seconds and checks that `ticks` increased. This is wall-clock coupled and would be flaky under extreme load. Low probability but worth noting.
- The `reader.loop()` finally-close path is not tested when `reader.read()` raises; the `finally` runs, but no test proves `close()` is called on error. Low risk since `SegmentReader.close()` just closes an mmap.

---

```yaml
findings:
  - id: TST-F1
    persona: testability-hawk
    title: Metric gauge values not asserted; wrong key or arithmetic is invisible
    severity: MEDIUM
    confidence: CONFIRMED
    location: tests/test_metrics.py
    dedupe_key: metrics-untested-values
    recommended_disposition: recommend-fix
    rationale: Seven of nine gauge callbacks have no test asserting the numeric value; a wrong dict key would yield an empty series with no test failure.
    revisit_condition: null
    fix_effort: small

  - id: TST-F2
    persona: testability-hawk
    title: Auth rejection path has no HTTP-surface test; deferred to P1 per this.i
    severity: MEDIUM
    confidence: LIKELY
    location: tests/test_app.py
    dedupe_key: controlplane-untested-auth
    recommended_disposition: recommend-defer
    rationale: No middleware calls verify_request; when P1 auth lands it needs rejection tests that the current suite provides no template for.
    revisit_condition: when auth middleware is added (this.i @s6v3qm P1)
    fix_effort: medium

  - id: TST-F3
    persona: testability-hawk
    title: Smoke test _free_port() TOCTOU race can steal the ephemeral port
    severity: LOW
    confidence: CONFIRMED
    location: tests/test_smoke.py:20
    dedupe_key: smoke-flaky-port-race
    recommended_disposition: recommend-fix
    rationale: Port is freed before subprocess binds it; concurrent process can steal the number, causing opaque "never came up" failure.
    revisit_condition: null
    fix_effort: small

  - id: TST-F4
    persona: testability-hawk
    title: Two pragma:no-cover branches hide untested guards from the 100% gate
    severity: LOW
    confidence: CONFIRMED
    location: src/witness/inloop.py:110
    dedupe_key: timedDoist-untested-null-sink
    recommended_disposition: recommend-fix
    rationale: sink=None and _kb(None) paths are unreachable in production but untested; removing the pragmas and adding trivial tests is safer than leaving them dark.
    revisit_condition: null
    fix_effort: small

  - id: TST-F5
    persona: testability-hawk
    title: vitals.runner_vitals() resident_bytes field is not asserted; wrong arithmetic invisible
    severity: LOW
    confidence: CONFIRMED
    location: tests/test_vitals.py:83
    dedupe_key: vitals-untested-resident-bytes
    recommended_disposition: recommend-fix
    rationale: The metric callback reads resident_bytes, but the unit test only asserts vm_rss_bytes; a wrong page-size multiplier or field index would go undetected.
    revisit_condition: null
    fix_effort: small
```
