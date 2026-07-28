"""witness error taxonomy — every failure is a typed WitnessError with a stable code, a
retryable flag, and a plain-sentence message, and serializes to the frozen error body."""

import pytest

from witness import errors

_ALL = [
    (errors.DbUnavailable, "witness.db.unavailable", True),
    (errors.IdentityUnavailable, "witness.identity.unavailable", True),
    (errors.InvalidArguments, "witness.config.invalid", False),
]


@pytest.mark.parametrize("cls,code,retryable", _ALL)
def test_each_error_is_a_witnesserror_with_a_stable_code_and_flag(cls, code, retryable):
    assert issubclass(cls, errors.WitnessError)
    assert cls.code == code
    assert cls.retryable is retryable
    err = cls("A complete sentence describing the failure.")
    assert isinstance(err, Exception)


@pytest.mark.parametrize("cls,code,retryable", _ALL)
def test_to_dict_renders_the_frozen_error_body(cls, code, retryable):
    err = cls("A complete sentence describing the failure.")
    assert err.to_dict() == {
        "code": code,
        "message": "A complete sentence describing the failure.",
        "retryable": retryable,
    }


def test_base_error_has_neutral_defaults():
    err = errors.WitnessError("Something specific happened.")
    assert err.code == "witness.error"
    assert err.retryable is False
    assert err.to_dict() == {
        "code": "witness.error",
        "message": "Something specific happened.",
        "retryable": False,
    }


def test_codes_are_unique():
    codes = [code for _, code, _ in _ALL]
    assert len(set(codes)) == len(codes)
