# Maintainability Review: bakobo/witness

**Date / Effort / Commit:** 2026-09-04 / medium / 2108341 (main)

## What a stranger would meet

The first thing a cold reader encounters is a tight, well-cross-referenced codebase. Every non-obvious decision points to a `this.i` node; every `~tick` mark in a comment traces to a closed or open `.tick/` entry; the in-loop module has a test that actively enforces its constraints. The overall shape — two processes, one image, mmap telemetry, read-only LMDB — is explained in-place at every callsite where it matters.

The non-obvious constructs a stranger would stop at:

- `_resolve_existing_db` and the two-step `Baser(reopen=False)` / `rdb.reopen(readonly=True)` open sequence in `reader.py`. Both are explained by the `~5s3e` tick and the inline comment on the keripy bug; the rationale is present and precise.
- `_is_witness_hab` + `_select_witness_hab`: the non-transferable filter is explained by `~2lmg` and `errors.py`'s docstrings. Closed tick.
- `timed()` in `inloop.py`: the PEP 479 `StopIteration` trap, the priming yield placement, and the four hio invariants are all documented in the docstring; `@vpu373to` records the design rationale.
- `TimedDoist.enter` + `_wrap`: the override-enter-only strategy is explained and correct.
- `SegmentWriter.mark_enter` / `mark_exit` and the seqlock: the format header comment at the top of `telemetry.py` is precise and the invariant is tested.
- `_key_states` in `reader.py`: the `isinstance(keys, tuple)` guard has **no rationale** — this is the finding where a well-meaning cleanup would break something (F2).
- `vitals.py`: raises `DbUnavailable` with database-specific class metadata for a process-not-found condition (F1).
- `runner.py._telemetry_doer()`: sets `self._writer` as a side effect used by the `run()` caller — temporal coupling with no name (F4).

The test coverage, 100% branch, is rigorous. Contract tests guard the keripy accessors; the hot-path freeze test guards the in-loop surface; transparency tests guard the `timed()` wrapper. This review found no raw `TODO`/`FIXME`, no commented-out blocks, and no stale docstrings.

## Executive summary

The code is legible cold — rationale is recorded at the point of confusion, the taxonomy is internally consistent, and the non-obvious patterns each have a marker. The sharpest "looks-like-a-mistake-but-isn't" trap is the two-step keripy DB open, but it is documented. The two issues that a stranger would most plausibly act on incorrectly are the `isinstance(keys, tuple)` guard in `_key_states` (no comment, removal looks like a cleanup) and the use of `DbUnavailable` in `vitals.py` for a process-not-found condition (the error title actively lies to API consumers). The most dangerous future-proofing trap is the `16` magic number used as the slot stride in `telemetry.py`, which `segment_size()` already does correctly via `_SLOT.size`.

## Assessment

**1. Absent why** — Two gaps. `_key_states`' `isinstance` guard has no rationale and invites removal (F2). `runner._telemetry_doer` sets `self._writer` as a side effect with only an ordering justification for the surrounding structure, not for the side-effect itself (F4). Everything else is marked or ticked.

**2. Naming** — `DbUnavailable` is raised in `vitals.py` for a condition with no database involved. The class's `title` ("I could not open the witness database.") and `code` ("e.env.witnessdb.unavailable.r") both lie to consumers of `/v1/witness/process` (F1).

**3. Idiom** — No findings. The code is idiomatic Python 3.14: `dataclass(frozen=True)`, `match`-free because the branching is too small to benefit, `struct.Struct` for the binary format, `mmap` with `PROT_READ`, `argparse` with a typed override. No frozen-in-time constructs.

**4. Encapsulation and separation of concerns** — Clean. The hot-path boundary is structural (separate module, freeze test). The reader process touches nothing that could write. The error taxonomy is centralized. No leaking internals.

**5. Duplication and needless complexity** — Two instances. The slot stride `16` is a magic number repeated in `mark_exit` and `_snapshot` where `segment_size()` already uses `_SLOT.size` (F3). Port validation is inlined in `_control_plane_config` while `_runner_config` uses the `_port()` helper for the same check (minor, not in top five).

**6. Debt hygiene** — Clean. No raw `TODO`/`FIXME`, no commented-out code, no stale docstrings. The `_on_retry = None` class attribute in `SegmentReader` is a test seam with no marker — a stranger might read it as dead code (F5).

**7. Future-proofing** — The `16` slot-stride magic number is the main risk: adding a field to `_SLOT` updates `segment_size()` automatically but silently corrupts `mark_exit` and `_snapshot` (F3). The keripy pin is fresh (`366d8107`), contract-tested, and the keripy-drift workflow guards it. The `uv.lock` is committed. The `heti` rename canary (`test_the_verdict_is_still_named_verifyresult`) is a model of forward-planning.

## Top findings

### F1: `DbUnavailable` raised for process-not-found — MEDIUM / CONFIRMED / `src/witness/vitals.py:52`

`DbUnavailable.title` is `"I could not open the witness database."` and its code is `"e.env.witnessdb.unavailable.r"`. Both attributes are set at class definition time and are the same for every instance, so they appear verbatim in the RFC 9457 `problem()` envelope at `/v1/witness/process` even when the actual condition is "the runner process is not running." A consumer of that endpoint sees the title "I could not open the witness database" for a process absence. A future maintainer who correctly reads `DbUnavailable` as scoped to database failures would add a separate `ProcessUnavailable` error rather than reuse this one — which would leave the existing misuse in place and permanently diverge the code from the taxonomy the errors module defines. The `code` segment `witnessdb` is also wrong: a process-not-found condition has nothing to do with a database.

**Wrong move:** A maintainer extending the endpoint taxonomy adds `class ProcessUnavailable(WitnessError)` correctly and forgets to update `vitals.py`, which keeps emitting a database error for a process condition.

**Recommendation:** Add `ProcessUnavailable(WitnessError)` with code `e.env.runner.unavailable.r` (or whichever taxonomy fits), title `"The witness runner process is not available."`, status 503, and raise it in `vitals.py` instead of `DbUnavailable`. The retryability (`.r`) is correct — the runner will restart — only the class identity and its baked-in strings are wrong.

### F2: `_key_states` — `isinstance(keys, tuple)` guard with no rationale — MEDIUM / CONFIRMED / `src/witness/reader.py:326`

```python
def _key_states(rdb):
    for keys, state in rdb.states.getTopItemIter():
        yield (keys[0] if isinstance(keys, tuple) else keys), state
```

The guard handles the case where `getTopItemIter()` returns a compound key tuple (the normal case — `keys[0]` is the AID) versus a plain string (the fallback). There is no comment explaining when keripy returns which, nor which keripy store type produces tuples vs plain values, nor what would happen if the guard were absent. A stranger refactoring this would almost certainly remove the `isinstance` branch as speculative generality — "it always returns a tuple" — and break the iterator for any store configuration where a plain key is returned. If `getTopItemIter()` returns a plain string and the guard is gone, `keys[0]` yields the first character of the AID rather than the AID itself, producing a silently wrong result rather than an error.

**Wrong move:** Stranger simplifies to `yield keys[0], state` and breaks controllers endpoint for any keripy store that returns plain keys.

**Recommendation:** Add a one-line comment: `# states.getTopItemIter may return a tuple of key segments or a plain string depending on the store type; keys[0] is the AID in the tuple case.` This makes the branch into a named guard rather than noise.

### F3: Slot stride `16` hardcoded instead of `_SLOT.size` — MEDIUM / CONFIRMED / `src/witness/telemetry.py:154,222`

`segment_size()` correctly computes `slots * _SLOT.size` (line 71), but `SegmentWriter.mark_exit` (line 154) and `SegmentReader._snapshot` (line 222) hardcode `16` as the per-slot stride:

```python
# mark_exit
pack_into("<dd", self._map, self._body + _SLOTS_OFF + slot * 16, elapsed, highest)
# _snapshot
_SLOT.unpack_from(self._map, self._body + _SLOTS_OFF + index * 16)
```

`_SLOT = struct.Struct("<dd")` has size 16 today. Adding a third field (say a call count) changes `_SLOT.size` to 24, and `segment_size()` updates automatically — the segment is created with the correct size. But the two hardcoded `16` values in the writer and reader remain, so `mark_exit` writes into the wrong position for every slot past the first, and `_snapshot` reads from the wrong position. The segment is silently corrupted and misread with no error.

**Wrong move:** Maintainer adds a field to `_SLOT`, updates `struct.Struct` and `segment_size()`, verifies the segment file is the right size, and ships silently corrupt per-slot data.

**Recommendation:** Replace both `16` literals with `_SLOT.size`. Two-character change each, zero risk.

### F4: `runner._telemetry_doer()` sets `self._writer` as an unnamed side effect — LOW / LIKELY / `src/witness/runner.py:89`

`WitnessRunner.run()` calls `self._telemetry_doer(doers)` and then immediately uses `self._writer`:

```python
doers.append(self._telemetry_doer(doers))
doist = self._doist_factory(..., sink=self._writer)
```

`self._writer` is not an `__init__` attribute — it is created inside `_telemetry_doer`:

```python
def _telemetry_doer(self, doers):
    names = inloop.TimedDoist.names_for(doers) + ["TelemetryDoer"]
    self._writer = self._create_segment(self._config.telemetry_path, names)
    return inloop.TelemetryDoer(writer=self._writer, tock=TOCK)
```

The method's comment explains the name-table ordering constraint (why the segment is created inside this helper rather than before it) but says nothing about the `self._writer` side effect that `run()` depends on. A stranger refactoring `run()` — for example, inlining `_telemetry_doer` or reordering the `doist_factory` call — would hit `AttributeError: 'WitnessRunner' object has no attribute '_writer'` with no clear error message pointing to the ordering constraint.

**Wrong move:** Stranger moves the `doist_factory` call above `_telemetry_doer` and gets `AttributeError` with no explanation in the code of where `_writer` is set.

**Recommendation:** Either return `(writer, doer)` from `_telemetry_doer` and let `run()` unpack it explicitly, or add a sentence: `# Also sets self._writer, which run() passes as sink.` The explicit return is cleaner.

### F5: `SegmentReader._on_retry` — unmarked test seam reads as dead code — LOW / CONFIRMED / `src/witness/telemetry.py:168`

```python
class SegmentReader:
    _on_retry = None
    ...
    def read(self, attempts=5):
        ...
        if self._on_retry is not None:
            self._on_retry()
```

`_on_retry` is never set outside test code (line 84 of `test_telemetry.py`: `reader._on_retry = writer._end_write`). There is no comment marking it as a test injection point. A stranger reading `SegmentReader` sees a class attribute that is always `None` in production, a check that always short-circuits, and code whose only branch is never reachable in normal use. They'd either (a) remove the attribute and the check as dead code, breaking the seqlock-race test, or (b) wonder why `_on_retry` exists without finding an answer in the module.

**Wrong move:** Stranger removes `_on_retry = None` and the `if self._on_retry` block as unreachable, breaks `test_a_read_that_races_one_write_still_succeeds`.

**Recommendation:** Add a one-line comment: `_on_retry = None  # test seam: tests inject a callable here to simulate a write arriving mid-read.`

## What's done well

The intent tree (`this.i`) is the best I have seen in this codebase family. Every load-bearing decision — the two-process split, the co-location rationale, the read-only open sequence, the seqlock format, the hot-path freeze — has a node with a `why` that rebuts the alternatives. The cross-references in code comments (e.g. `@c7v3kp`, `@k3p7wr`, `~5s3e`) are consistently placed at the exact line where a stranger would need the explanation. The keripy-drift CI job and the contract tests in `test_keripy_contract.py` and `test_heti_contract.py` are the right way to guard an unforked upstream dependency. The `hot_path` decorator + `test_the_hot_path_stays_frozen` is a textbook example of a structural guarantee enforced by a test rather than a note. The error taxonomy is principled and self-consistent within the `errors.py` module.

## Residual unknowns

The `isinstance(keys, tuple)` guard (F2) requires keripy source knowledge to confirm which store configurations return plain keys vs tuples — the finding is filed as MEDIUM on the "a stranger would remove it" criterion, but the precise keripy behavior in the edge branch was not verified against keripy source in this pass. A deep reading of `keri.db.subing.MapSuber.getTopItemIter` would settle it.

---

## Findings manifest

```yaml
findings:
  - id: MNT-F1
    persona: maintainability-expert
    title: DbUnavailable raised for process-not-found, title and code lie
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/witness/vitals.py:52
    dedupe_key: vitals-divergent
    recommended_disposition: recommend-fix
    rationale: >
      DbUnavailable.title says "I could not open the witness database." and its
      code says "e.env.witnessdb.unavailable.r"; both surface verbatim in RFC 9457
      responses from /v1/witness/process for a process-not-found condition.
      A future maintainer extending the taxonomy would correctly not reuse this class
      for a process error, leaving the existing misuse permanent.
    revisit_condition: null
    fix_effort: small

  - id: MNT-F2
    persona: maintainability-expert
    title: _key_states isinstance guard has no rationale — looks removable
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/witness/reader.py:326
    dedupe_key: key-states-missing
    recommended_disposition: recommend-fix
    rationale: >
      The `isinstance(keys, tuple)` guard in _key_states handles a keripy
      getTopItemIter return-value shape with no comment explaining when it fires
      or what breaks without it; a stranger refactoring would remove it and get
      silently wrong AID values (first character of the string) rather than an error.
    revisit_condition: null
    fix_effort: small

  - id: MNT-F3
    persona: maintainability-expert
    title: Slot stride hardcoded as 16 instead of _SLOT.size in write and read paths
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/witness/telemetry.py:154
    dedupe_key: telemetry-duplicated
    recommended_disposition: recommend-fix
    rationale: >
      segment_size() uses _SLOT.size correctly; mark_exit (line 154) and _snapshot
      (line 222) hardcode 16. Adding a field to _SLOT updates the segment size but
      leaves both access offsets wrong, silently corrupting per-slot telemetry data.
    revisit_condition: null
    fix_effort: small

  - id: MNT-F4
    persona: maintainability-expert
    title: _telemetry_doer() sets self._writer as unnamed side effect used by caller
    severity: LOW
    confidence: LIKELY
    location: src/witness/runner.py:89
    dedupe_key: runner-coupled
    recommended_disposition: recommend-fix
    rationale: >
      run() uses self._writer immediately after calling _telemetry_doer(); _writer
      is not set in __init__ and its assignment inside _telemetry_doer() is not
      named or mentioned in the comment. A stranger reordering run() or inlining
      _telemetry_doer() hits AttributeError with no contextual explanation.
    revisit_condition: null
    fix_effort: small

  - id: MNT-F5
    persona: maintainability-expert
    title: SegmentReader._on_retry is an unmarked test seam that reads as dead code
    severity: LOW
    confidence: CONFIRMED
    location: src/witness/telemetry.py:168
    dedupe_key: telemetry-missing
    recommended_disposition: recommend-fix
    rationale: >
      _on_retry is always None in production and is set only in test_telemetry.py.
      No comment marks it as a test injection point. A stranger reads the always-None
      class attribute and the guarded call as dead code and removes both, breaking
      the seqlock-race test.
    revisit_condition: null
    fix_effort: small
```
