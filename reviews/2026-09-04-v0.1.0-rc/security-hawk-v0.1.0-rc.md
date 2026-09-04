# Security Review: witness

**Date / Effort / Commit:** 2026-09-04 / deep (unattended) / `212afdb`

## Attack surface enumerated

- **HTTP entry points (control plane, falcon over waitress).** Nine read-only `GET` routes under `/v1/witness/*`: `health`, `identity`, `version`, `loop`, `escrow`, `database`, `process`, `controller` (list), `controller/{aid}`. No authentication in this release (auth is `@s6v3qm`, deferred to P1 by `@t3k6ps`/`@h5n2rk`). Only untrusted HTTP input is the `{aid}` path segment and the `Bakobo-Request-Id` request header. No mutation endpoints, no request bodies parsed, no bytes ever written to the store — the control plane opens keripy's LMDB `MDB_RDONLY` and closes it per request (`reader.py`).
- **Trust decisions.** `_is_witness_hab` / `_select_witness_hab` (which hab is "our" identity), the wedge-duration check in `health()`, and the seqlock torn-read detection in `SegmentReader.read`. No signature verification is performed in v1 (nothing consequential is authorized yet).
- **Sinks.** No `exec`/`system`/`eval`/`shell=True` anywhere in `src/`. `subprocess.Popen(list(argv))` in the supervisor is argv-form with operator-supplied commands (no shell). No SQL, no templating, no deserialization of untrusted bytes.
- **Secret paths.** No `logging`/`print`/telemetry of key material anywhere in `src/` (grep-confirmed: the package logs nothing at all). Private keys / passcodes are never read or served by the control plane; `--passcode` is only forwarded to keripy by the runner.
- **Supply chain.** `uv.lock` is fully hash-pinned; the image builds `uv sync --frozen`. keri and heti are pinned by 40-char git commit. Every GitHub Action is pinned by commit SHA. CI auth uses the `bakobo-dependency-reader` GitHub App (short-lived token), not a PAT; `persist-credentials: false` is set where the `insteadOf` rewrite is in play. All workflows trigger on `pull_request` (not `pull_request_target`); event data is passed through `env:`, never spliced into shell.
- **Boundary & isolation.** Two co-located processes, one PID namespace (`@a24p3kbw`); control plane runs non-root (uid 1001); LMDB read-only enforced at the library level; the in-loop surface is frozen by an AST test (`@e3uji3mv`).

## Executive summary

This is an unusually security-conscious codebase: fail-closed throughout, no key material on any served path, the production-critical witness structurally insulated from the control plane, and a supply chain that is genuinely locked down. The residual risk is concentrated not in the Python but in the **deployment contract**: the whole confinement of the unauthenticated read-only surface rests on a single operator-supplied `-p 127.0.0.1` publish flag, and the one attacker-reachable path that touches the production-critical witness — the query-not-found escrow amplification — reaches it through the *publicly-proxied keripy port*, with the only compensating control (edge rate limiting) living in `infra` and described in this repo's own docs as unmeasured. Nothing here rises to CRITICAL or HIGH; the findings are hardening obligations against a design whose intentional tradeoffs are already recorded in `this.i`.

## Top findings

### F1: query-not-found escrow amplification degrades the production-critical witness, unauthenticated
- **Severity:** MEDIUM · **Confidence:** LIKELY · **Location:** `docs/deploying.md:63` (behavior is keripy's; the deployment contract is this repo's)
- **Attacker / path / effect:** An unauthenticated remote sends `/query` requests for AIDs this witness does not hold to the witness HTTP port (5631), which is published to the network through Caddy (`infra @m4c35y`). Each such query parks an entry in the query-not-found escrow, which `Kevery.processQueryNotFound` re-walks on **every** hio loop pass (~32 Hz). Sustaining N entries costs the attacker only ~N/300 req/s (entries age out after `TimeoutQNF` = 300 s), and a deep escrow degrades the single-threaded cooperative loop into slowness — directly harming the component `@c7v3kp` exists to protect. The witness's own docs state this cost is **unmeasured**.
- **Rubric:** #2 (untrusted input reaching a sink unbounded / parser-allocation DoS on attacker-sized load). **Recommendation:** Treat the edge rate limit as a load-bearing control, not advice: confirm a concrete per-source rate limit is configured in Caddy/`DOCKER-USER` before this ships to a witness that matters, and wire `witness.escrow.depth{store="query_not_found"}` to an alert (the repo already exposes it). This is an upstream keripy behavior bounded by `TimeoutQNF`, so it is a hardening obligation on the deployment rather than a witness code defect — but the compensating control is currently unverified.

### F2: control plane binds `0.0.0.0` unauthenticated; confinement is one publish-flag deep
- **Severity:** MEDIUM · **Confidence:** CONFIRMED · **Location:** `Dockerfile:106`, `docs/deploying.md:34`
- **Attacker / path / effect:** The image CMD runs `witness control-plane --host 0.0.0.0 --port 5633` with no auth. The intended confinement is entirely the operator's host publish flag `-p 127.0.0.1:5633:5633`. A single fat-finger (`-p 5633:5633`, or a compose file that omits the loopback host) exposes the whole read-only surface to the network — witness identity, every witnessed controller's key state, escrow depths, database internals, and runner process vitals — to any unauthenticated caller. Docker's published-port rules also bypass the `INPUT` chain, so a host firewall written there will *look* like a second layer and silently do nothing (the docs call this out).
- **Rubric:** #1 (authority by network position), #6 (over-broad exposure). **Recommendation:** This is a recorded, intentional tradeoff (`@h5n2rk`/`@t3k6ps`: v1 is read-only, low-sensitivity, no key material, auth lands in P1), so it is **recommend-accept-risk**, not a blocker. Defense-in-depth worth taking now: bind the container listener to a non-`0.0.0.0` address is not possible cross-container, but the image could refuse to serve unless an explicit `--allow-unauthenticated` (or equivalent) flag is passed, converting the silent-exposure default into a deliberate one — the same "make the mistake unavailable rather than discouraged" stance `@lnk24kwp` took with `latest`.

### F3: recommended `--nopasscode` keystore leaves the witness signing key unencrypted at rest
- **Severity:** LOW · **Confidence:** CONFIRMED · **Location:** `docs/deploying.md:26`
- **Attacker / path / effect:** The bootstrap the docs prescribe is `kli init --name witness --nopasscode`, so the witness's private signing key sits in the `witness-data` volume with no passcode-derived encryption. Anyone with read access to that volume (a second container mounting it, a host-level compromise, a backup/snapshot, a misconfigured volume driver) obtains the witness's private key and can forge witness receipts for that AID — which is the witness's entire security function.
- **Rubric:** #3 (key material at rest). **Recommendation:** Largely inherent: a witness must sign autonomously with no human to type a passcode, so the key must be usable at boot. Keep it, but state the compensating control explicitly in the deploy contract — the key's confidentiality reduces entirely to volume/host access control (keripy's `0o1700` tree perms and the non-root uid help but do not encrypt) — so backups and volume mounts of `witness-data` are key-material-grade secrets and must be treated as such.

### F4: attacker-controlled `Bakobo-Request-Id` reflected unbounded into header and error body
- **Severity:** LOW · **Confidence:** CONFIRMED · **Location:** `src/witness/app.py:53`
- **Attacker / path / effect:** `RequestIdMiddleware` takes the caller's `Bakobo-Request-Id` header verbatim, with no length bound or charset check, and echoes it back into the response header and into the `request_id` field of every problem+json error. CRLF header-splitting is mitigated downstream (waitress rejects CRLF in header values), so the realistic effect is limited: correlation-id forgery, and — if any downstream consumer keys logs/traces by this id — log/trace injection or confusion. No amplification of note (waitress caps header size).
- **Rubric:** #2 (untrusted value echoed to a sink without a cap). **Recommendation:** Only accept a client-supplied request id if it matches a tight pattern (e.g. `[A-Za-z0-9._-]{1,128}`); otherwise mint a fresh `uuid4().hex`. Cheap, and it keeps a forged id from ever entering an operator's correlation trail.

## Lower-severity notes

- **`controllers()` returns the full key-state list in one unbounded response** (`reader.py:267`). Bounded by how many controllers the witness actually witnesses (not directly attacker-set), and a control-plane memory spike cannot degrade the witness (`@c7v3kp`; the supervisor restarts it). Worth a page/limit if the witness set ever grows large, but not exploitable to consequence today.
- **`falcon`/`waitress`/`opentelemetry-*` carry no version floor in `pyproject.toml`.** Reproducibility is nonetheless sound because `uv.lock` hash-pins them and the image builds `--frozen`; the only exposure is a `uv lock` regeneration silently pulling a yanked/backdoored newer release, which the hash-pinned lock already guards against for existing builds. Consider minimum floors when a security fix in one of them needs to be asserted.
- **No `SECURITY.md`.** Not a vulnerability, but a repo shipping a signing component with a deferred-auth surface benefits from a stated disclosure path and an explicit "control plane is unauthenticated in v1" warning at the top level, not only inside `deploying.md`.

## What's done well

- The control plane cannot corrupt or stall the witness *by construction*, and the two-step `Baser(reopen=False)` + `reopen(readonly=True)` dance closes a real keripy footgun (`reader.py:144`) rather than trusting the obvious-but-wrong `readonly=True` constructor path.
- The in-loop surface is frozen against accretion by an AST test (`@e3uji3mv`), which is the right mechanism for a "one more small doer" failure mode a docstring cannot stop.
- Supply chain is genuinely locked: hash-pinned lockfile, SHA-pinned actions, git-commit-pinned deps, GitHub-App short-lived tokens (no PAT), `persist-credentials: false`, BuildKit secret for the build-time token so it never enters a layer or `docker history`. Workflows use `pull_request` (not `_target`) and pass event data through `env:`.
- Fail-closed error taxonomy: retryability is *derived* from the code so it cannot drift, `ForeignKeystore` refuses to report someone else's AID as the witness's, and metric callbacks degrade a down witness to *no data* rather than to an exception.
- No logging of anything, so no accidental secret-in-logs path exists.

## Residual unknowns

- **The qnf-escrow latency-vs-query-rate curve (F1) is unmeasured**, by the repo's own admission. The check that would settle severity is a load test: sustain a `/query` flood at a fixed rate against a live witness and measure `witness.loop.lag` and `witness.escrow.depth{store="query_not_found"}` — and confirm the Caddy/`DOCKER-USER` rate limit actually caps it.
- **Whether waitress in the pinned version rejects all CRLF/control characters in echoed header values (F4)** was reasoned from waitress's general behavior, not exercised here; a single request with an embedded `\r\n` in `Bakobo-Request-Id` against the running image would confirm the header-splitting mitigation holds.
- **Whether keripy's `--nopasscode` keeper leaves the seed fully plaintext or lightly obfuscated (F3)** was not traced into keripy; either way volume access is the control, so the recommendation stands, but the exact at-rest representation affects how loudly to state it.

## Findings manifest

```yaml
findings:
  - id: SEC-F1
    persona: security-hawk
    title: query-not-found escrow amplification degrades the witness, unauthenticated
    severity: MEDIUM
    confidence: LIKELY
    location: docs/deploying.md:63
    dedupe_key: witness-unbounded-on-query-flood
    recommended_disposition: recommend-defer
    rationale: unauth /query flood to the public witness port grows a per-pass-walked escrow, degrading the single-threaded loop; compensating edge rate limit lives in infra and is unmeasured
    revisit_condition: edge rate limit confirmed configured in Caddy/DOCKER-USER and the latency-vs-query-rate cost measured
    fix_effort: small
  - id: SEC-F2
    persona: security-hawk
    title: control plane binds 0.0.0.0 unauthenticated; confinement is one publish-flag deep
    severity: MEDIUM
    confidence: CONFIRMED
    location: Dockerfile:106
    dedupe_key: control-plane-unauthenticated
    recommended_disposition: recommend-accept-risk
    rationale: a single `-p 127.0.0.1` slip exposes the whole read-only surface; recorded tradeoff (v1 read-only, no key material, auth in P1) but the default silently fails open
    revisit_condition: null
    fix_effort: small
  - id: SEC-F3
    persona: security-hawk
    title: recommended --nopasscode keystore leaves witness signing key unencrypted at rest
    severity: LOW
    confidence: CONFIRMED
    location: docs/deploying.md:26
    dedupe_key: keystore-leaky-at-rest
    rationale: volume/host read access yields the witness private key and thus forged receipts; largely inherent to an autonomous signer, so the control is volume/host access hygiene
    recommended_disposition: recommend-accept-risk
    revisit_condition: null
    fix_effort: small
  - id: SEC-F4
    persona: security-hawk
    title: attacker-controlled Bakobo-Request-Id reflected unbounded into header and error body
    severity: LOW
    confidence: CONFIRMED
    location: src/witness/app.py:53
    dedupe_key: request-id-unbounded
    rationale: client-supplied correlation id echoed verbatim with no cap/charset check; CRLF split mitigated by waitress, residual is id forgery / downstream log confusion
    recommended_disposition: recommend-defer
    revisit_condition: a downstream consumer keys logs/traces by request_id, or the value reaches any log sink
    fix_effort: small
```
