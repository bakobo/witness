"""The error taxonomy and its RFC 9457 envelope (@zzbdxa).

Codes follow ``<sorter>.<descriptor>[.<sub>].<disposition>`` from dev/standards/error-codes.md,
classified by what the *obstacle* was rather than by which component raised it, with retryability
in the trailing token so a caller can prefix-match a whole branch of meaning. The envelope is
RFC 9457 problem+json, per dev/standards/http-errors.md.

The earlier codes (``witness.db.unavailable`` and friends) predate all four standards and are
re-cut here rather than grandfathered, which is exactly what @zzbdxa decided.
"""

import re

import pytest

from witness import errors


ALL = [
    errors.DbUnavailable,
    errors.WitnessNotIncepted,
    errors.ForeignKeystore,
    errors.InvalidArguments,
    errors.TelemetryUnavailable,
    errors.TelemetryIncompatible,
    errors.TelemetryNotConfigured,
    errors.RunnerNotRunning,
    errors.ControllerUnknown,
]

# sorter . descriptor . at least one sub-descriptor . disposition
_CODE = re.compile(r"^[ew]\.[a-z]+(?:\.[a-z]+)+\.[rf]$")


@pytest.mark.parametrize("kind", ALL)
def test_every_code_is_well_formed(kind):
    assert _CODE.match(kind.code), f"{kind.__name__} has a malformed code: {kind.code!r}"


@pytest.mark.parametrize("kind", ALL)
def test_no_code_is_a_bare_descriptor(kind):
    """`e.proof.` names a category, never one condition's identity — so a code always carries at
    least one sub-descriptor."""
    assert len(kind.code.split(".")) >= 4


def test_codes_are_unique_across_the_taxonomy():
    """A static title is per code, so two conditions sharing a code cannot both be described."""
    codes = [kind.code for kind in ALL]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("kind", ALL)
def test_retryability_lives_in_the_trailing_token(kind):
    assert kind.retryable is kind.code.endswith(".r")


@pytest.mark.parametrize("kind", ALL)
def test_every_error_has_a_static_title_that_is_a_whole_sentence(kind):
    assert kind.title.endswith("."), f"{kind.__name__}'s title is not a sentence"
    assert kind.title[0].isupper()


def test_the_envelope_is_rfc9457_problem_details():
    exc = errors.DbUnavailable("The database at /x could not be opened.")

    problem = exc.problem(instance="/v1/witness/health", request_id="01K1M4YQ8ZP3V7")

    assert problem == {
        "code": "e.env.witnessdb.unavailable.r",
        "type": "https://errors.bakobo.com/e.env.witnessdb.unavailable.r",
        "title": errors.DbUnavailable.title,
        "detail": "The database at /x could not be opened.",
        "instance": "/v1/witness/health",
        "request_id": "01K1M4YQ8ZP3V7",
    }


def test_the_type_url_is_derived_mechanically_from_the_code():
    for kind in ALL:
        problem = kind("detail.").problem(instance="/x", request_id="r")
        assert problem["type"] == f"https://errors.bakobo.com/{kind.code}"


def test_an_envelope_omits_correlation_members_it_was_not_given():
    """A request id is not always available — a CLI failure has none — and an empty string in the
    envelope would be worse than the member's absence."""
    problem = errors.InvalidArguments("Bad flag.").problem(instance=None, request_id=None)

    assert "instance" not in problem
    assert "request_id" not in problem


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (errors.DbUnavailable, 503),
        (errors.TelemetryUnavailable, 503),
        (errors.WitnessNotIncepted, 409),
        (errors.ForeignKeystore, 500),
        (errors.TelemetryIncompatible, 500),
        (errors.InvalidArguments, 400),
        (errors.TelemetryNotConfigured, 501),
        (errors.RunnerNotRunning, 503),
        (errors.ControllerUnknown, 404),
    ],
)
def test_the_status_follows_the_codes_prefix(kind, status):
    """dev/standards/http-errors.md maps prefixes to statuses; two codes with the same descriptor
    always map to the same status, so the mapping belongs to the code and not to the call site."""
    assert kind.status == status


def test_a_pending_witness_is_distinguished_from_a_foreign_keystore():
    """~2lmg: a witness that has not been incepted yet is transient and resolves on its own; a
    keystore that belongs to somebody else never will. Sharing one code hid that difference."""
    assert errors.WitnessNotIncepted.retryable is True
    assert errors.ForeignKeystore.retryable is False
    assert errors.WitnessNotIncepted.status != errors.ForeignKeystore.status


def test_a_missing_capability_is_not_reported_as_a_broken_dependency():
    """DX-F1 from the v0.1.0-rc panel. /loop used to raise DbUnavailable when the control plane
    was started with --no-telemetry, so an operator read "I could not open the witness database"
    about a database that was fine. The obstacle is a capability this deployment does not offer,
    which is e.feature., not e.env."""
    assert errors.TelemetryNotConfigured.code.startswith("e.feature.")
    assert errors.TelemetryNotConfigured.retryable is False
    assert "database" not in errors.TelemetryNotConfigured.title.lower()


def test_a_stopped_witness_process_is_not_reported_as_a_database_failure():
    """MNT-F1, the same mistake at the other call site: vitals.runner_vitals raised
    DbUnavailable when the process was absent, which names the wrong component."""
    assert errors.RunnerNotRunning.code.startswith("e.env.runner")
    assert errors.RunnerNotRunning.retryable is True
    assert "database" not in errors.RunnerNotRunning.title.lower()


def test_situational_values_are_carried_as_args_for_clients_that_render_their_own_text():
    """DX-F2. http-errors.md lists `args` as an envelope member precisely so a client can
    localise or reformat without parsing English out of `detail`."""
    problem = errors.ControllerUnknown("No state for BAid.", args=["BAid"]).problem()

    assert problem["args"] == ["BAid"]


def test_args_is_omitted_when_an_error_has_no_situational_values():
    """An empty list in the envelope is noise; RFC 9457 members are optional for a reason."""
    assert "args" not in errors.DbUnavailable("Nothing to say.").problem()
