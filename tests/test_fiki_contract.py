"""Contract tests over fiki, the RFC 9421 verifier @s6v3qm rides.

Same bargain as ``tests/test_keripy_contract.py``, for the same reason: witness commits to
somebody else's function and nothing else checks that the function still has the shape witness
expects. This file replaced ``test_heti_contract.py`` when @s6v3qm moved off heti — the bundle
these symbols live in was ``heti.l0``, then ``heti.ephemeral``, and is now a library of its own.
That history is the argument for keeping a tripwire rather than trusting the import.

The Registrar and the control plane both call fiki now, so these tests pin what both call sites
assume. Their value is that a pin bump fails here, naming the assumption that moved, rather than
somewhere downstream — which is what happened at the py/v0.9.0 bump, where ``authorities``
became required. The py/v0.10.0 bump moved nothing witness calls; its changes are stricter input
checks (header lists, keyid shape, IP-literals) and messages that quote values safely.
"""

import inspect

import pytest
from fiki import Verdict, errors as fiki_errors, verify_request


def test_verify_request_takes_the_arguments_witness_will_pass():
    """A falcon request supplies method, url and headers directly. ``max_age`` has no default in
    fiki and must be decided at the call site, which is the argument witness has to think about
    rather than inherit — so it is asserted here as required, not merely present. ``authorities``
    joined it in py/v0.9.0, and @34sgjmxr is where the control plane decides it."""
    sig = inspect.signature(verify_request)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    required = {name for name, p in sig.parameters.items() if p.default is inspect.Parameter.empty}
    assert required == {"method", "url", "headers", "max_age", "authorities"}
    assert {"body", "expected_aid", "skew", "now"} <= set(sig.parameters)


def test_the_verdict_carries_the_aid_an_allowlist_is_checked_against():
    """@s6v3qm's authorization is an operator-AID allowlist, so ``aid`` is the field the whole
    decision turns on. ``covered`` names what the signature actually spanned, which is the other
    half: an AID proven over components that exclude the path proves less than it appears to."""
    assert set(Verdict.__dataclass_fields__) == {"aid", "covered", "keyid"}


def test_the_verdict_asserts_no_freshness_so_witness_must_pass_max_age():
    """CANARY for a shape change with a security consequence. heti's verdict carried a
    ``timestamp``; fiki's deliberately does not, because it declines to imply a freshness
    guarantee it has not made. If a timestamp ever appears here, the replay reasoning at the call
    site has to be revisited rather than quietly inheriting a new field."""
    assert "timestamp" not in Verdict.__dataclass_fields__


def test_fiki_raises_typed_exceptions_and_carries_no_bakobo_error_codes():
    """The consequence @s6v3qm accepted, pinned so it cannot change without notice. heti mapped
    fiki's classes onto ``e.input.*``/``e.proof.*`` at its boundary; witness now owns that
    translation. If fiki ever grows a ``code`` attribute the translation should be reconsidered,
    and this test is where that shows up."""
    assert issubclass(fiki_errors.MissingSignature, fiki_errors.FikiError)
    assert not hasattr(fiki_errors.FikiError, "code")
    assert not hasattr(fiki_errors, "matches")


def test_verify_rejects_a_transferable_aid_rather_than_best_effort_checking_it():
    """A negative requirement, given a positive oracle (ledger #22). fiki's identifiers are
    non-transferable by construction — the AID IS the Ed25519 verifying key — so a transferable
    ``E`` prefix is anchored territory and must be refused, not approximated. Nothing else in
    witness would notice if this stopped holding."""
    transferable = "EK7ZUmFebD2st48Yvtzc9LajV3Yg2mkeeDzVRL-hhrpg"  # 'E' prefix: transferable
    headers = {
        "signature-input": f'sig=("@method" "@target-uri");created=1;keyid="{transferable}"',
        "signature": "sig=:AAAA:",
    }
    with pytest.raises(fiki_errors.FikiError):
        verify_request(
            method="GET",
            url="https://witness.example/v1/witness/health",
            headers=headers,
            max_age=None,
            authorities=None,
        )


def test_verify_refuses_a_body_its_signature_does_not_cover():
    """The Registrar relies on this rather than checking it again (registrar/app.py ``_signer``),
    because a second check behind fiki's could never fire and so could never be covered. That
    makes this test the guard: if a later fiki stops refusing an uncovered body under its default
    minimum, it fails here instead of the Registrar quietly accepting an unbound body."""
    from fiki import Key, sign_request

    url = "https://registrar.example/v1/registrar/publication"
    headers = sign_request(key=Key.generate(), method="POST", url=url)
    with pytest.raises(fiki_errors.FikiError):
        verify_request(method="POST", url=url, headers=headers, body=b'{"a":1}', max_age=None,
                       authorities=None)


def test_the_authority_a_signature_binds_is_the_address_the_caller_dialled():
    """@0h9r92wk leaves ``--authority`` unset in production because, over an SSH tunnel, the
    ``@authority`` a caller signs is the tunnel's local end rather than anything naming the
    witness. That reasoning holds only while fiki reads the authority from the URL as signed and
    refuses one outside ``authorities``; if either stops being true, the decision is revisited
    here rather than discovered in production."""
    from fiki import Key, sign_request

    dialled = "http://127.0.0.1:15633/v1/witness/loop"
    headers = sign_request(key=Key.generate(), method="GET", url=dialled)
    verdict = verify_request(method="GET", url=dialled, headers=headers, max_age=60,
                             authorities=("127.0.0.1:15633",))
    assert "@authority" in verdict.covered
    with pytest.raises(fiki_errors.SignatureMismatch):
        verify_request(method="GET", url=dialled, headers=headers, max_age=60,
                       authorities=("witness.example:5633",))
