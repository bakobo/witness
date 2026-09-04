# DevOps Review: bakobo/witness
**Date / Effort / Commit:** 2026-09-04 / medium / 212afdb (HEAD on main, v0.1.0-rc milestone)

## Delivery machinery examined

**Repo type:** Service with a container delivery surface. The unit of deployment is a Docker image published to GHCR and consumed by `bakobo/infra` by digest.

**CI workflows** (`.github/workflows/`):
- `ci.yml` — runs `uv run pytest` (100% branch-coverage gate) on push to main and on PRs. Mints a GitHub App token for the private `heti` dependency.
- `image.yml` — builds the image, runs an image oracle (`tests/test_image_smoke.py`), and on non-PR pushes publishes the image to GHCR.
- `keripy-drift.yml` — weekly scheduled canary that retests against keripy `main` to surface upstream drift early.
- `image-retention.yml` — weekly scheduled retention report; applies only on manual dispatch with `apply=true`.
- `copilot-review-gate.yml` — adds/removes Copilot as PR reviewer based on title/label conventions.

**Test command (per AGENTS.md):** `uv run pytest` — `--cov=witness --cov-branch --cov-fail-under=100` (configured in `pyproject.toml`).

**Lockfile:** `uv.lock` committed. Dependency closure is deterministic when used frozen.

**Container:** Two-stage Dockerfile (`python:3.14-slim-bookworm` base, non-root `witness` uid 1001, no compiler in runtime stage, no secrets baked in). `uv` pinned at `0.9.7` in the builder stage.

**Release path:** Automated. `image.yml` builds, validates with the oracle, and publishes on every push to main and on version tags. infra deploys by digest (documented in `docs/deploying.md`). No manual release steps outside of updating `bakobo/infra`'s consumed digest.

**Config/secrets:** No committed secrets found. OTEL endpoint configurable via `OTEL_EXPORTER_OTLP_ENDPOINT` (optional; documented in README). GitHub App token for `heti` access uses the `bakobo-dependency-reader` App pattern (no PAT). The `gh_token` BuildKit secret passes the token into the builder layer without it landing in any image layer or `docker history`.

**Action pinning:** All three third-party actions pinned by commit SHA and verified at node24 runtime: `actions/checkout@d23441a…` (v6, node24), `astral-sh/setup-uv@37802a…` (v7, node24), `actions/create-github-app-token@bcd2ba…` (v3, node24).

## Executive summary

The gate machinery is largely sound: CI runs the repo's test command, enforces 100% branch coverage, and uses the right tools (frozen lockfile in the container build, SHA-pinned actions, non-root image). The most important gap is that the image publication workflow (`image.yml`) runs in parallel with — not after — the test workflow (`ci.yml`), so a commit whose unit tests fail can still result in a published image if the image oracle passes. Three medium-weight hygiene gaps follow: CI installs dependencies without `--frozen`, the base image is not pinned by digest, and the Dockerfile declares no `HEALTHCHECK` despite a documented health endpoint purpose-built for exactly this signal.

## Assessment

1. **Gate enforced.** Mostly yes. `ci.yml` runs the full test suite with the 100%-branch coverage gate; no `continue-on-error` on the test step; no excluded paths. The one real gap is that `image.yml` is independent of `ci.yml` — both trigger simultaneously on push to main, so a push whose tests fail in `ci.yml` can still produce and publish an image via `image.yml` if the oracle passes. This is the top finding.

2. **Build supply chain.** All GitHub Actions pinned by SHA and confirmed node24. `uv` pinned to `0.9.7` in the Dockerfile. keripy and heti pinned to exact git SHAs. `GITHUB_TOKEN` is `contents: read` by default, with `packages: write` added only in the image job. One gap: `uv sync` in `ci.yml` and `image.yml` does not pass `--frozen`, unlike the Dockerfile which correctly uses `uv sync --frozen`. The Dockerfile base image is `python:3.14-slim-bookworm` without a digest pin — mutable.

3. **Reproducible, source-traceable release.** Good. Every build is tagged `sha-<full-commit>`; release tags add semver. No `:latest`. infra documented to deploy by digest. The image workflow publishes the OCI digest to the job summary. Release steps are fully automated in `image.yml` — no tribal knowledge.

4. **Container/compose hygiene.** Good in most dimensions: multi-stage build, non-root uid, no secrets baked in, `BuildKit --secret` correctly used, `org.opencontainers.image.*` labels populated. One gap: no `HEALTHCHECK` instruction. A container orchestrator cannot distinguish "process started" from "healthy" without it, and the service has a purpose-built health endpoint.

5. **Config/secrets.** No committed secrets. OTEL exporter endpoint documented in README with an example. `DEP_READER_PRIVATE_KEY` stored as a repository secret (not leaked). No `.env.example` file, but the only runtime-configurable value (`OTEL_EXPORTER_OTLP_ENDPOINT`) is documented and optional with a documented no-op default.

6. **Observability/operability.** The health endpoint (`GET /v1/witness/health`) correctly distinguishes a wedged loop from a healthy one — better than a simple port check. Metrics emit over OTLP when configured. The `docs/deploying.md` names the series worth alerting on. The runtime health story is solid; the container-level health signal (Docker HEALTHCHECK) is absent.

7. **Migrations/restart-safety.** The LMDB volume holds the witness's identity and KEL; it persists across container restarts and is documented as required. The upgrade path (new image, same volume) is explicitly noted in `docs/deploying.md` as "not yet proven": whether a `kli migrate run` step is ever needed between keripy pins has not been exercised. No migration tooling or runbook is present.

## Top findings

### F1: Image publication not gated on CI test suite — a test-failing commit can publish — HIGH / CONFIRMED / `.github/workflows/image.yml` + `.github/workflows/ci.yml`

`ci.yml` and `image.yml` both trigger on `push: branches: [main]` and run in parallel with no dependency between them. A commit whose unit tests fail (ci.yml exits non-zero) still proceeds through image.yml if the image oracle passes. The oracle is a smoke test — it confirms the image starts, answers health, and registers its LMDB reader. It does not run the full suite. A unit regression that the oracle doesn't exercise would produce a published GHCR image from a broken commit.

**Recommendation:** Change `image.yml`'s trigger to `workflow_run` on `ci.yml` completion with `conclusion: success`, or add an explicit `needs:` if the two workflows are merged into one. The image oracle then becomes a second-layer gate rather than the only one.

### F2: `uv sync` without `--frozen` in CI — lockfile discipline not mechanically enforced — MEDIUM / CONFIRMED / `.github/workflows/ci.yml:55`, `.github/workflows/image.yml:99`

The Dockerfile correctly uses `uv sync --frozen --no-dev --no-editable`, which fails if `pyproject.toml` and `uv.lock` are inconsistent. `ci.yml` and `image.yml` both run bare `uv sync`, which will silently update the lockfile rather than failing if the two diverge. A commit that modifies `pyproject.toml` without running `uv lock` can pass CI while installing a dependency set that differs from what `uv.lock` documents.

**Recommendation:** Add `--frozen` to both `uv sync` calls in CI: `uv sync --frozen`. Lockfile drift then fails loudly at the right moment.

### F3: Dockerfile base image mutable — builds not fully reproducible — MEDIUM / CONFIRMED / `Dockerfile:19`, `Dockerfile:57`

Both `FROM python:3.14-slim-bookworm` lines use a floating tag. A Debian security patch or Python patch release between two builds silently produces a different image from identical source, violating the reproducibility the `sha-<commit>` tag and digest-based deployment otherwise provide.

**Recommendation:** Pin both `FROM` lines by digest: `FROM python:3.14-slim-bookworm@sha256:<current-digest> AS builder` (and the same for the runtime stage). Update the digest whenever a new Python patch ships. The AGENTS.md `infra.instructions.md` already lists "base image without a pinned tag" as a critical finding.

### F4: No HEALTHCHECK in Dockerfile — orchestrators cannot distinguish healthy from started — MEDIUM / CONFIRMED / `Dockerfile`

The Dockerfile has no `HEALTHCHECK` instruction. `GET /v1/witness/health` is documented and purpose-built: it checks that the database opens AND the hio loop is not wedged (returning `degraded` when a doer holds the loop past five seconds). A container orchestrator that relies on Docker's built-in health mechanism would treat the container as healthy the moment the process starts, remaining blind to a wedged loop.

**Recommendation:** Add a `HEALTHCHECK` using `curl` or the equivalent against `/v1/witness/health` with a start period (the witness needs time to initialize the database before the first probe) and an interval suitable for the loop-wedge signal (e.g., 30 s interval, 5 s timeout, 3 retries, 60 s start period).

### F5: Upgrade path (LMDB across version bumps) not tested or documented as runnable — MEDIUM / CONFIRMED / `docs/deploying.md:71`

`docs/deploying.md` explicitly states: "Two things are not yet proven and should be before the first one that matters: bringing a new image up on an older image's LMDB without re-incepting, and whether a `kli migrate run` step is ever needed between pins. Neither has been exercised here." A keripy pin bump that requires a migration step would, without prior testing, result in a silent data failure or a broken start on the live volume.

**Recommendation:** Before the first production upgrade, exercise the upgrade path in a test environment with a real LMDB volume: bring the old image up, populate state, bring the new image up on the same volume, and verify the witness starts and holds keys correctly. Record the outcome (pass/need-migration/incompatible) and add it to `docs/deploying.md`. If a `kli migrate run` step is required, encode it as a named runbook step or a one-off container command in the deploy docs, not tribal knowledge.

## What's done well

The supply-chain posture for actions is excellent: every action is pinned by full commit SHA, all three are confirmed at the node24 runtime, and no action is pinned by mutable tag. The `bakobo-dependency-reader` App pattern is the right pattern for private intra-org dependencies; the lack of a PAT and the use of BuildKit secrets for the token are both correct. The image publishing workflow is disciplined: no `:latest`, every build tagged `sha-<commit>`, and infra's digest-based deploy removes the floating-tag risk entirely. The keripy-drift canary is a thoughtful addition that catches upstream divergence before it accumulates. The control-plane health endpoint is purpose-built for the real failure mode (wedged loop, not just closed port), which is better than most services achieve.

## Residual unknowns

**Branch protection.** Whether pushes directly to `main` require a passing CI status check is not determinable from the codebase. The git log shows direct commits on `main`. If branch protection does not require CI to pass, the F1 parallel-publish gap applies to all pushes, not just edge cases. F1's recommended fix (`workflow_run` trigger) is the right remedy regardless of branch protection state.

**keripy-drift canary failure notification.** `keripy-drift.yml` runs on a schedule and reports drift via `::notice::` annotations and console output, but there is no Slack/email/issue-creation step on failure. A drift failure would only be noticed if someone monitors the Actions dashboard. This is LOW severity — the main gate is the keripy pin in `ci.yml`, not the drift canary — but worth a mention.

---

## Findings manifest

```yaml
findings:
  - id: OPS-F1
    persona: devops-engineer
    title: Image published in parallel with CI — test-failing commit can ship as a container
    severity: HIGH
    confidence: CONFIRMED
    location: .github/workflows/image.yml
    dedupe_key: github-actions-missing-gate
    recommended_disposition: recommend-fix
    rationale: ci.yml and image.yml trigger simultaneously on push to main; a commit whose unit tests fail in ci.yml still publishes an image if the image oracle passes
    revisit_condition: null
    fix_effort: small

  - id: OPS-F2
    persona: devops-engineer
    title: uv sync without --frozen in CI — lockfile discipline not enforced
    severity: MEDIUM
    confidence: CONFIRMED
    location: .github/workflows/ci.yml:55
    dedupe_key: ci-lockfile-unfrozen
    recommended_disposition: recommend-fix
    rationale: Bare uv sync can silently install deps diverging from uv.lock; Dockerfile already uses --frozen correctly
    revisit_condition: null
    fix_effort: small

  - id: OPS-F3
    persona: devops-engineer
    title: Dockerfile base image uses mutable tag, not digest — builds not reproducible
    severity: MEDIUM
    confidence: CONFIRMED
    location: Dockerfile:19
    dedupe_key: dockerfile-unpinned-base
    recommended_disposition: recommend-fix
    rationale: python:3.14-slim-bookworm resolves to a different SHA after a Debian/Python patch; two builds from the same commit produce different images
    revisit_condition: null
    fix_effort: small

  - id: OPS-F4
    persona: devops-engineer
    title: No HEALTHCHECK in Dockerfile — orchestrators cannot distinguish healthy from started
    severity: MEDIUM
    confidence: CONFIRMED
    location: Dockerfile
    dedupe_key: dockerfile-missing-healthcheck
    recommended_disposition: recommend-fix
    rationale: GET /v1/witness/health exists and distinguishes wedged loops from healthy ones; without HEALTHCHECK, orchestrators mark the container healthy on process start
    revisit_condition: null
    fix_effort: small

  - id: OPS-F5
    persona: devops-engineer
    title: Upgrade path across keripy pins (LMDB migration) not tested or encoded
    severity: MEDIUM
    confidence: CONFIRMED
    location: docs/deploying.md:71
    dedupe_key: upgrade-path-untested
    recommended_disposition: recommend-fix
    rationale: deploying.md explicitly states the LMDB-across-versions upgrade path has not been exercised; first production upgrade risks silent data failure or broken start
    revisit_condition: After the upgrade path is exercised in a test environment and the outcome is documented in deploying.md.
    fix_effort: medium
```
