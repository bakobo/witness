"""Contract tests over heti, the other dependency witness rides at v0.0.0.

Same bargain as ``tests/test_keripy_contract.py``, for the same reason. ``s6v3qm`` commits witness
to heti's RFC 9421 verifier rather than reimplementing 9421, and ``f5j3wc`` commits witness and
heti to sharing exactly one keripy pin. Both commitments are load-bearing and neither is checked
anywhere else.

heti is moving fast: the bundle these symbols live in was called ``l0`` until recently and is now
``ephemeral``, which already made ``s6v3qm``'s citation of ``heti.l0.verify_request`` stale. These
tests are the tripwire for the next such move.
"""

import inspect
import pathlib
import tomllib

import pytest
from heti import errors as heti_errors
from heti import ephemeral

_LOCK = pathlib.Path(__file__).resolve().parent.parent / "uv.lock"


def _lock():
    return tomllib.load(_LOCK.open("rb"))


def test_keri_resolves_to_exactly_one_pin_shared_with_heti():
    """f5j3wc: witness depends on keri directly AND on heti, which pins keri itself. Two
    different git refs for `keri` cannot co-resolve, so a heti bump that moves its keripy pin
    must fail here rather than at somebody's `uv sync`."""
    packages = _lock()["package"]
    keri = [p for p in packages if p["name"] == "keri"]
    assert len(keri) == 1, "keri resolved more than once; heti and witness pins have diverged"
    dependents = {p["name"] for p in packages if any(d["name"] == "keri" for d in p.get("dependencies", []))}
    assert {"heti", "witness"} <= dependents


def test_verify_request_takes_the_arguments_witness_will_pass():
    """s6v3qm rides this function. Its parameters are keyword-only and are exactly what a falcon
    request can supply without witness reshaping anything."""
    sig = inspect.signature(ephemeral.verify_request)
    assert list(sig.parameters) == ["method", "path", "headers"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())


def test_a_verdict_carries_the_aid_covered_fields_and_timestamp():
    """The verdict is what authorization reads: `aid` is the proven non-transferable AID that an
    operator allowlist is checked against."""
    fields = ephemeral.VerifyResult.__dataclass_fields__
    assert set(fields) == {"aid", "covered", "timestamp"}


def test_the_verdict_is_still_named_verifyresult():
    """CANARY, expected to fail on a heti bump rather than on a defect here.

    heti's own this.i records the verdict as renamed to `EphemeralVerdict` ("renamed from
    VerifyResult on @lg5q7y's vocabulary"), and the code has not caught up. When it does, this
    test fails and tells us to follow the rename deliberately instead of discovering it through
    an AttributeError somewhere in the auth path.
    """
    assert hasattr(ephemeral, "VerifyResult")
    assert not hasattr(ephemeral, "EphemeralVerdict"), (
        "heti has landed the EphemeralVerdict rename recorded in its this.i — update witness's "
        "use of the verdict type and this canary together."
    )


def test_verify_rejects_a_transferable_aid_rather_than_best_effort_checking_it():
    """A negative requirement, given a positive oracle (ledger #22). Ephemeral verification
    accepts only non-transferable AIDs; a transferable one is anchored territory and must be
    refused, not approximated. Nothing else in witness would notice if this stopped holding."""
    transferable = "EK7ZUmFebD2st48Yvtzc9LajV3Yg2mkeeDzVRL-hhrpg"  # 'E' prefix: transferable
    headers = {
        "signify-resource": transferable,
        "signature-input": 'signify=("@method" "@path");created=1',
        "signature": "indexed=?0",
    }
    with pytest.raises(heti_errors.HetiError) as caught:
        ephemeral.verify_request(method="GET", path="/v1/witness/health", headers=headers)
    # The exact code, not merely "some heti error" — otherwise this passes just as happily when
    # the signature fails to parse, and the rejection it claims to prove never runs.
    assert caught.value.code == "e.feature.unsupported.trans-aid.f"


def test_error_matching_is_prefix_based_so_witness_can_handle_whole_branches():
    """error-codes.md:94 puts the matcher in one place precisely so repos do not reimplement it.
    witness will match on prefixes rather than enumerate leaves."""
    assert heti_errors.matches("e.env.witness-db.r", "e.env.")
    assert not heti_errors.matches("e.input.range.f", "e.env.")
    assert heti_errors.matches("e.input.range.f", "e.*.f")
