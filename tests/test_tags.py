"""The tag vocabulary, its operator door, and the derivation onto an AID (@vqqh6zdk).

Two things are under test here and they fail differently. The door is a boundary check, graded
against dev/standards/input-handling.md: every refusal branch needs its own case, because a door
whose reject paths are unexercised is a door nobody has opened. The derivation is pure arithmetic
over a witness set, and what it has to get right is monotonicity — @dhh2gnvv makes a testnet
marking something a consumer never clears, so a union that ever *loses* a tag is the bug that
matters.

`from_peer` is deliberately absent until the signed reply route lands. @k3tkkss2 records that the
two doors differ (an operator's unknown bare name is a typo; a peer's is a newer vocabulary), but
writing the peer door now would mean shipping a function with no caller, which is the same claim
the pyproject note refuses to make about fiki.
"""

import pytest

from witness import tags
from witness.errors import InvalidArguments


def test_testnet_is_the_defined_vocabulary():
    """The vocabulary is exactly one name, and `production` is deliberately not in it.

    @pmtzkn6j: a witness asserting it is production makes a self-serving claim nothing verifies.
    This asserts the absence as well as the presence, because the absence is the decision.
    """
    assert tags.TESTNET == "testnet"
    assert set(tags.KNOWN) == {"testnet"}
    assert "production" not in tags.KNOWN
    assert tags.KNOWN[tags.TESTNET]


class TestOperatorDoor:
    """Everything the operator can hand us, and what happens at each bound."""

    def test_none_is_no_tags(self):
        assert tags.from_operator(None) == ()

    def test_empty_is_no_tags(self):
        assert tags.from_operator([]) == ()

    def test_a_known_name_is_admitted(self):
        assert tags.from_operator(["testnet"]) == ("testnet",)

    def test_a_vendor_prefixed_name_is_admitted_without_being_known(self):
        """@k3tkkss2's extension point: a dotted prefix is anyone's to mint."""
        assert tags.from_operator(["bakobo.pool"]) == ("bakobo.pool",)

    def test_output_is_sorted_and_deduplicated(self):
        """A response member is a contract, so its order cannot depend on argv order."""
        assert tags.from_operator(["testnet", "bakobo.pool", "testnet"]) == (
            "bakobo.pool",
            "testnet",
        )

    def test_too_many_is_refused(self):
        with pytest.raises(InvalidArguments) as caught:
            tags.from_operator(["bakobo.t%d" % n for n in range(tags.MAX_TAGS + 1)])
        assert str(tags.MAX_TAGS) in str(caught.value)

    def test_exactly_the_maximum_is_admitted(self):
        """The bound is inclusive; off-by-one here would refuse a legitimate configuration."""
        supplied = ["bakobo.t%d" % n for n in range(tags.MAX_TAGS)]
        assert len(tags.from_operator(supplied)) == tags.MAX_TAGS

    def test_too_long_is_refused(self):
        with pytest.raises(InvalidArguments) as caught:
            tags.from_operator(["bakobo." + "a" * tags.MAX_TAG_LENGTH])
        assert str(tags.MAX_TAG_LENGTH) in str(caught.value)

    def test_exactly_the_length_limit_is_admitted(self):
        name = "bakobo." + "a" * (tags.MAX_TAG_LENGTH - len("bakobo."))
        assert tags.from_operator([name]) == (name,)

    @pytest.mark.parametrize(
        "malformed",
        [
            "Testnet",  # upper case
            "-testnet",  # leading hyphen
            "test net",  # whitespace
            "test_net",  # underscore
            "9lives",  # leading digit
            "bakobo.",  # trailing dot, so an empty segment
            ".pool",  # leading dot, so an empty segment
            "bakobo..pool",  # doubled dot
            "",  # nothing at all
        ],
    )
    def test_malformed_shape_is_refused(self, malformed):
        with pytest.raises(InvalidArguments):
            tags.from_operator([malformed])

    def test_an_unknown_bare_name_is_refused(self):
        """An operator's unknown bare name is a typo, and startup is where to catch it.

        A misspelled `testnet` that silently fails to apply is the dangerous failure: the witness
        comes up looking production-grade and nothing says otherwise.
        """
        with pytest.raises(InvalidArguments) as caught:
            tags.from_operator(["testnetz"])
        assert "testnetz" in str(caught.value)

    def test_a_non_string_is_refused_rather_than_raising(self):
        """Rubric item 3: the validator is total, so a hostile type is a refusal not a TypeError."""
        with pytest.raises(InvalidArguments):
            tags.from_operator([5])

    def test_refusal_names_the_bound_without_echoing_the_input(self):
        """Rubric item 6: the reader gets the rule, not their own payload back."""
        flood = "bakobo." + "z" * 500
        with pytest.raises(InvalidArguments) as caught:
            tags.from_operator([flood])
        assert flood not in str(caught.value)


class TestDerive:
    """Tag inheritance from an AID's witness set, and the honesty of a partial answer."""

    def test_no_witnesses_yields_nothing(self):
        derived = tags.derive(witnesses=[], known={})
        assert derived.tags == ()
        assert derived.resolved == ()
        assert derived.unresolved == ()

    def test_an_untagged_witness_taints_nothing(self):
        derived = tags.derive(witnesses=["B1"], known={"B1": ()})
        assert derived.tags == ()
        assert derived.resolved == ("B1",)
        assert derived.unresolved == ()

    def test_a_testnet_witness_taints_the_aid(self):
        derived = tags.derive(witnesses=["B1"], known={"B1": ("testnet",)})
        assert derived.tags == ("testnet",)
        assert derived.resolved == ("B1",)

    def test_one_testnet_witness_among_many_is_enough(self):
        """The rule is monotone: any testnet witness taints, and no quorum excuses it."""
        derived = tags.derive(
            witnesses=["B1", "B2", "B3"],
            known={"B1": (), "B2": ("testnet",), "B3": ()},
        )
        assert derived.tags == ("testnet",)

    def test_tags_from_several_witnesses_union(self):
        derived = tags.derive(
            witnesses=["B1", "B2"],
            known={"B1": ("testnet",), "B2": ("bakobo.pool",)},
        )
        assert derived.tags == ("bakobo.pool", "testnet")

    def test_an_unknown_witness_is_reported_unresolved_not_assumed_clean(self):
        """@nlunqygr: a partial answer is the honest one, so `unresolved` is first-class."""
        derived = tags.derive(witnesses=["B1", "B2"], known={"B1": ("testnet",)})
        assert derived.tags == ("testnet",)
        assert derived.resolved == ("B1",)
        assert derived.unresolved == ("B2",)

    def test_every_witness_unknown_yields_no_tags_and_full_unresolved(self):
        """Absence is never assurance (@pmtzkn6j) — empty tags here means 'we cannot say'."""
        derived = tags.derive(witnesses=["B1", "B2"], known={})
        assert derived.tags == ()
        assert derived.unresolved == ("B1", "B2")

    def test_a_repeated_witness_is_counted_once(self):
        derived = tags.derive(witnesses=["B1", "B1"], known={"B1": ("testnet",)})
        assert derived.resolved == ("B1",)
        assert derived.tags == ("testnet",)

    def test_ordering_is_stable_regardless_of_witness_order(self):
        """Two orderings of the same witness set must give byte-identical answers."""
        first = tags.derive(witnesses=["B2", "B1"], known={"B1": ("testnet",), "B2": ()})
        second = tags.derive(witnesses=["B1", "B2"], known={"B1": ("testnet",), "B2": ()})
        assert first == second
