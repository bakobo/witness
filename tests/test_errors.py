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
