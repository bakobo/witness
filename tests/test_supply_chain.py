"""The image's supply chain: signing, provenance and the SBOM (@4dx8zyjr).

None of this can run in a unit test — keyless signing needs GitHub's OIDC token — so what is pinned
here is the shape that makes it trustworthy, which is also the shape that is easy to erode by a
well-meaning edit: the signing permission lives on one job that never runs for a pull request,
every downloaded binary is checked against a hash, the verify command a deployer is told to run
pins the identity it ought to, and retention cannot delete a signature out from under a release.
The workflow itself proves the signature verifies, on every push, by running cosign against the
digest it just signed.
"""

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
IMAGE_YML = ROOT / ".github" / "workflows" / "image.yml"
RETENTION_YML = ROOT / ".github" / "workflows" / "image-retention.yml"
DEPLOYING = ROOT / "docs" / "deploying.md"

ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW = "https://github.com/bakobo/witness/.github/workflows/image.yml"


def _workflow(path=IMAGE_YML):
    # PyYAML reads the bare key `on` as True; neither test below needs the triggers by name.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _signing_job():
    jobs = _workflow()["jobs"]
    signing = [name for name, job in jobs.items() if "id-token" in job.get("permissions", {})]
    assert len(signing) == 1, f"exactly one job may mint certificates, found {signing}"
    return signing[0], jobs[signing[0]]


def _run_text(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


class TestTheSigningJob:
    def test_it_is_the_only_job_with_id_token_and_never_runs_for_a_pull_request(self):
        name, job = _signing_job()
        assert job["permissions"]["id-token"] == "write"
        assert job["permissions"]["attestations"] == "write"
        assert job["if"] == "github.event_name != 'pull_request'"
        assert job["needs"] == "image", "it signs what the image job pushed, after the oracle"

    def test_the_workflow_default_grants_nothing_to_sign_with(self):
        assert _workflow()["permissions"] == {"contents": "read"}

    def test_it_signs_attests_and_verifies_the_digest(self):
        _, job = _signing_job()
        text = _run_text(job)
        assert "cosign sign --yes" in text
        assert "cosign verify" in text, "the job proves its own signature verifies"
        assert f"--certificate-oidc-issuer {ISSUER}" in text
        attests = [step for step in job["steps"] if step.get("uses", "").startswith("actions/attest@")]
        assert len(attests) == 2, "one provenance attestation and one SBOM attestation"
        assert all(step["with"]["push-to-registry"] is True for step in attests)
        assert all("subject-digest" in step["with"] for step in attests)
        assert sum("sbom-path" in step["with"] for step in attests) == 1

    def test_every_downloaded_binary_is_checked_against_a_pinned_hash(self):
        _, job = _signing_job()
        text = _run_text(job)
        downloads = re.findall(r"curl [^\n]*https://\S+", text)
        assert len(downloads) == 2, "cosign and syft, nothing else fetched"
        assert len(re.findall(r"\b[0-9a-f]{64}\b", text)) == 2
        assert text.count("sha256sum --check --strict") == 2


def test_every_action_is_pinned_by_commit_and_none_is_docker():
    for path in (IMAGE_YML, RETENTION_YML):
        for job in _workflow(path)["jobs"].values():
            for step in job["steps"]:
                if "uses" not in step:
                    continue
                ref = step["uses"]
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref
                assert not ref.startswith("docker/"), ref


def _documented_verify():
    commands = re.findall(
        r"^cosign verify .*?(?<!\\)$", re.sub(r"\s*\\\n\s*", " ",
                                             DEPLOYING.read_text(encoding="utf-8")), flags=re.M
    )
    assert len(commands) == 1, "deploying.md should give exactly one cosign verify command"
    argv = shlex.split(commands[0])
    return dict(zip(argv[3::2], argv[4::2], strict=False)), argv


class TestTheDocumentedVerifyCommand:
    def test_it_pins_the_github_issuer_and_a_digest(self):
        flags, argv = _documented_verify()
        assert flags["--certificate-oidc-issuer"] == ISSUER
        assert argv[2].startswith("ghcr.io/bakobo/witness@sha256:")

    @pytest.mark.parametrize("ref", ["refs/tags/v1.2.3", "refs/tags/v0.10.0"])
    def test_it_accepts_a_release_tag(self, ref):
        pattern = _documented_verify()[0]["--certificate-identity-regexp"]
        assert re.search(pattern, f"{WORKFLOW}@{ref}")

    @pytest.mark.parametrize("identity", [
        f"{WORKFLOW}@refs/heads/main",
        f"{WORKFLOW}@refs/pull/7/merge",
        f"{WORKFLOW}@refs/tags/v1.2.3-rc1",
        f"{WORKFLOW}@refs/tags/v1.2.3\nextra",
        "https://github.com/bakobo/witness/.github/workflows/ci.yml@refs/tags/v1.2.3",
        "https://github.com/evil/witness/.github/workflows/image.yml@refs/tags/v1.2.3",
        f"https://github.com/bakobo/witness/.github/workflows/image.yml@refs/tags/v1.2.3x",
        f"https://evil.example/{WORKFLOW}@refs/tags/v1.2.3",
    ])
    def test_it_refuses_every_other_identity(self, identity):
        pattern = _documented_verify()[0]["--certificate-identity-regexp"]
        assert not re.search(pattern, identity)


@pytest.mark.skipif(shutil.which("jq") is None, reason="the retention plan is a jq program")
def test_retention_never_counts_a_signature_or_attestation_as_deletable():
    """A released image is kept forever, so its signature must be too, or the deploy gate would
    refuse a release that is fine. Runs the workflow's own jq program on a known listing."""
    step = _workflow(RETENTION_YML)["jobs"]["retention"]["steps"][0]["run"]
    program = re.search(r"jq --argjson keep \"\$\{KEEP\}\" '(.*?)' versions.json", step, re.S)[1]
    versions = [
        {"id": 1, "created_at": "2026-01-01", "metadata": {"container": {"tags": ["v1.0.0"]}}},
        {"id": 2, "created_at": "2026-01-02", "metadata": {"container": {"tags": ["sha-a"]}}},
        {"id": 3, "created_at": "2026-01-03", "metadata": {"container": {"tags": ["sha-b"]}}},
        {"id": 4, "created_at": "2026-01-04",
         "metadata": {"container": {"tags": ["sha256-" + "0" * 64]}}},
        {"id": 5, "created_at": "2026-01-05", "metadata": {"container": {"tags": []}}},
    ]
    plan = json.loads(subprocess.run(
        ["jq", "--argjson", "keep", "1", program], input=json.dumps(versions),
        capture_output=True, text=True, check=True,
    ).stdout)
    assert [entry["id"] for entry in plan["doomed"]] == [2]
    assert plan["released"] == 1 and plan["kept"] == 1
