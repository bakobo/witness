"""What a witness declares about itself, and what an AID inherits from it (@vqqh6zdk).

Two kinds of declaration, split by whether a consumer DECIDES on the value or merely DISPLAYS it
(@e4ceoopg). A **tag** is a predicate a consumer acts on, so it is a bare name from an agreed
vocabulary and it unions across an AID's witness set. An **attribute** is a key and a value a
consumer shows a human, so it needs no merge rule and no AID inherits one.

A tag is a bare name and the collection is a list, because the consumer question is always
membership — "does this witness claim X" — and a map would invite a schema argument per key
(@k3tkkss2). Bare lowercase names are the defined vocabulary below; a dotted vendor prefix is
anyone's to mint, which is what keeps an open namespace from collapsing into mutual
unintelligibility.

The vocabulary holds ``testnet`` and deliberately holds no ``production`` (@pmtzkn6j). A witness
asserting it is production makes a self-serving claim nothing verifies; asserting it is testnet
speaks against its own interest, which is the only kind of self-assertion worth reading. So the
answer is three-valued — tagged, untagged, and never production-asserted — and an empty tuple here
means "we cannot say", never "this is fine".

:func:`tags_from_operator` is a door in the sense of dev/standards/input-handling.md: size, then shape,
then meaning, each only trustworthy if the one before it ran. Its peer counterpart arrives with the
signed reply route, where an unknown bare name means a newer vocabulary rather than a typo and must
pass through opaquely.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import InvalidArguments

#: The one standard tag. Its meaning is COIA APPENDIX-D flag 6's, deliberately verbatim in sense:
#: an experimental, test or demonstration environment with no real-world consequence.
TESTNET = "testnet"

KNOWN_TAGS = {
    TESTNET: (
        "This witness belongs to an experimental, test or demonstration environment and carries "
        "no real-world consequence to reputation, governance or cost."
    ),
}

KNOWN_ATTRIBS = {
    "operator": "Who runs this witness, as a human-readable name.",
    "contact": "How to reach that operator about this witness — an address or a URL.",
    "pool": "The laboratory pool this witness belongs to, when it belongs to one.",
}

#: A flood guard, not an opinion about how many tags a witness legitimately has. Sixteen is far
#: past any real configuration and far below anything that would trouble the process.
MAX_TAGS = 16
MAX_ATTRIBS = 16
#: Likewise a guard. A name long enough to need 64 characters is not communicating.
MAX_TAG_LENGTH = 64
#: Values get more room than names because a contact URL legitimately needs it, and less than a
#: payload would because this is a line on a screen rather than a document.
MAX_VALUE_LENGTH = 256

#: A flood guard on the seed file. Eight kibibytes is far past 16 tags and 16 bounded attributes
#: and far below anything that would trouble the process to read or parse.
MAX_SEED_BYTES = 8192

#: The members a seed file may carry. Closed rather than open, because a typo'd member silently
#: ignored is a witness that declares less than its operator intended and says nothing about it.
_SEED_MEMBERS = ("tags", "attribs")

#: Attributes arrive as ``key=value``. Split once, never greedily: a contact is very often a URL
#: and a URL contains '=', so a greedy split would silently truncate the commonest real value.
_PAIR_SEPARATOR = "="

#: One segment: a letter, then letters, digits and hyphens. Segments join with dots. The inner
#: repetition is anchored on a literal dot that no segment character can match, so the pattern
#: cannot backtrack catastrophically — rubric item 3, since a door that can hang on hostile input
#: has turned a refusal into a denial of service.
_SEGMENT = r"[a-z][a-z0-9-]*"
_SHAPE = re.compile(rf"^{_SEGMENT}(?:\.{_SEGMENT})*$")


@dataclass(frozen=True)
class Derived:
    """What an AID's witness set says about it, and whom we could not ask.

    ``unresolved`` is a member rather than an omission because the control plane does not fetch
    peer witnesses (@nlunqygr): a partial answer stated as partial is honest, and a partial answer
    presented as complete is the failure this feature exists to prevent.
    """

    tags: tuple[str, ...]
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]


def _admit_name(value, known, kind):
    """Bound one name by size, then shape, then meaning, or refuse it.

    Shared by tag names and attribute keys on purpose: a key is a name, and making it obey exactly
    the same rule is what makes a key as interoperable as a tag (@e4ceoopg).
    """
    if not isinstance(value, str):
        raise InvalidArguments(f"Every {kind} must be text, but one of those supplied was not.")
    if len(value) > MAX_TAG_LENGTH:
        # The length, never the value: a refusal that echoes a 500-character argument back at the
        # operator has handed them their own payload instead of the rule (rubric item 6).
        raise InvalidArguments(
            f"A {kind} may be at most {MAX_TAG_LENGTH} characters, but one was {len(value)}."
        )
    if not _SHAPE.match(value):
        raise InvalidArguments(
            f"A {kind} must be lowercase letters, digits and hyphens in dot-separated segments, "
            "each beginning with a letter, as in 'testnet' or 'bakobo.pool'."
        )
    if "." not in value and value not in known:
        # Bare names are the shared vocabulary, so an unrecognized one is a typo rather than an
        # extension — and a misspelled `testnet` that silently fails to apply leaves the witness
        # looking production-grade with nothing to say otherwise.
        raise InvalidArguments(
            f"{value!r} is not a defined {kind}. The defined ones are "
            f"{', '.join(sorted(known))}; one of your own needs a vendor prefix, as in "
            "'bakobo.pool'."
        )
    return value


def _admit_value(value):
    """Bound one attribute value: non-empty, short, and printable ASCII only.

    The character restriction is the deliberate one (@e4ceoopg). A value is shown to a person, so
    a Cyrillic homograph or a bidi override is a spoofing surface rather than an
    internationalization win, and a newline in a field that reaches a log is a forged log line —
    the same concern app.py already applies to a caller-supplied request id. A field nobody is
    permitted to act on loses very little by being narrow.
    """
    if not isinstance(value, str):
        # Unreachable from the command line, where partition always yields text, and reachable
        # from a seed file, where JSON will happily hand over a number or a nested object.
        raise InvalidArguments("Every attribute value must be text, but one was not.")
    if not value:
        raise InvalidArguments(
            "An attribute value may not be empty; leave the key out instead, which says the same "
            "thing more clearly."
        )
    if len(value) > MAX_VALUE_LENGTH:
        raise InvalidArguments(
            f"An attribute value may be at most {MAX_VALUE_LENGTH} characters, but one was "
            f"{len(value)}."
        )
    if not all("\x20" <= character <= "\x7e" for character in value):
        raise InvalidArguments(
            "An attribute value must be printable ASCII, so that what a person reads on a screen "
            "is what the operator wrote."
        )
    return value


def tags_from_operator(values):
    """Door for tags the operator supplied. Returns them sorted and deduplicated.

    Sorted because the result reaches an HTTP response, and a contract whose member order depends
    on argv order is not a contract. Counted before deduplication, because the bound is on what
    crossed the boundary rather than on what survived it.
    """
    if values is None:
        return ()
    supplied = list(values)
    if len(supplied) > MAX_TAGS:
        raise InvalidArguments(
            f"At most {MAX_TAGS} tags may be supplied, but {len(supplied)} were."
        )
    return tuple(sorted({_admit_name(value, KNOWN_TAGS, "tag") for value in supplied}))


def attribs_from_operator(values):
    """Door for ``key=value`` attributes the operator supplied. Returns a dict.

    A repeated key is refused rather than resolved last-one-wins: silently letting argv order
    decide what a witness says about itself is the kind of invisible behaviour that is discovered
    only when it has already been wrong for a while.
    """
    if values is None:
        return {}
    supplied = list(values)
    if len(supplied) > MAX_ATTRIBS:
        raise InvalidArguments(
            f"At most {MAX_ATTRIBS} attributes may be supplied, but {len(supplied)} were."
        )
    admitted = {}
    for item in supplied:
        if not isinstance(item, str):
            raise InvalidArguments(
                "Every attribute must be text of the form key=value, but one was not text."
            )
        if _PAIR_SEPARATOR not in item:
            raise InvalidArguments(
                "Every attribute must be written key=value, but one had no '=' in it."
            )
        key, _, value = item.partition(_PAIR_SEPARATOR)
        if key in admitted:
            raise InvalidArguments(
                f"The attribute {key!r} was supplied more than once, and which one wins would "
                "otherwise depend on the order of the arguments."
            )
        admitted[key] = value
    return _admit_attribs(admitted)


def _admit_attribs(mapping):
    """Bound an already-unpacked attribute mapping, whatever door unpacked it."""
    if len(mapping) > MAX_ATTRIBS:
        raise InvalidArguments(
            f"At most {MAX_ATTRIBS} attributes may be supplied, but {len(mapping)} were."
        )
    return {
        _admit_name(key, KNOWN_ATTRIBS, "attribute"): _admit_value(value)
        for key, value in mapping.items()
    }


class _DuplicateMember(ValueError):
    """Raised through json.loads when an object repeats a member name."""

    def __init__(self, member):
        super().__init__(member)
        self.member = member


def _no_duplicate_members(pairs):
    """An ``object_pairs_hook`` that refuses a repeated member instead of keeping the last."""
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateMember(key)
        seen[key] = value
    return seen


@dataclass(frozen=True)
class Seed:
    """What a seed file declares. Empty members mean "declared nothing", never an error."""

    tags: tuple[str, ...] = ()
    attribs: dict = field(default_factory=dict)


def from_seed(text):
    """Door for a declaration seed file (@hjz7b7qo).

    Size first, and specifically before ``json.loads``: parsing a megabyte in order to discover it
    is too large has already paid the cost the bound exists to avoid.
    """
    if len(text) > MAX_SEED_BYTES:
        raise InvalidArguments(
            f"A declaration seed may be at most {MAX_SEED_BYTES} bytes, but one was {len(text)}."
        )
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_members)
    except _DuplicateMember as exc:
        # attribs_from_operator refuses a repeated key rather than letting argv order decide;
        # json.loads would silently collapse the same mistake to last-one-wins, so the two doors
        # would disagree about an invariant one of them states explicitly.
        raise InvalidArguments(
            f"The member {exc.member!r} appears more than once in the declaration seed, and "
            "which one wins would otherwise depend on the order they are written in."
        ) from None
    except ValueError:
        raise InvalidArguments(
            "A declaration seed must be JSON, and this one could not be parsed."
        ) from None
    if not isinstance(document, dict):
        raise InvalidArguments(
            "A declaration seed must be a JSON object with tags and attribs members."
        )
    for member in document:
        if member not in _SEED_MEMBERS:
            raise InvalidArguments(
                f"{member!r} is not something a declaration seed can carry. It holds "
                f"{' and '.join(_SEED_MEMBERS)}."
            )
    # Membership rather than .get(): an explicit null is a malformed value, not an absent
    # member, and collapsing the two would let {"tags": null} pass as "declared nothing".
    tags = document["tags"] if "tags" in document else []
    if not isinstance(tags, list):
        raise InvalidArguments("The tags member of a declaration seed must be a list of names.")
    attribs = document["attribs"] if "attribs" in document else {}
    if not isinstance(attribs, dict):
        raise InvalidArguments(
            "The attribs member of a declaration seed must be an object of key to value."
        )
    return Seed(tags=tags_from_operator(tags), attribs=_admit_attribs(attribs))


def derive(witnesses, known):
    """Union the tags of every witness in ``witnesses`` whose tags ``known`` holds.

    Monotone by construction: one tagged witness is enough, and no quorum of untagged ones excuses
    it. That is the whole rule — a witness set is only as production-ready as its least
    production-ready member, and @dhh2gnvv makes the resulting marking one the consumer never
    clears.

    ``witnesses`` is an AID's witness list (``state.b``); ``known`` maps a witness AID to its tags.
    Only the AID's own witnesses, deliberately: whether a delegated AID inherits from its
    delegator's witnesses is undecided (~4tml).
    """
    resolved = []
    unresolved = []
    collected = set()
    for aid in dict.fromkeys(witnesses):  # dedupe, and a repeated witness is still one witness
        if aid in known:
            resolved.append(aid)
            collected.update(known[aid])
        else:
            unresolved.append(aid)
    return Derived(
        tags=tuple(sorted(collected)),
        resolved=tuple(sorted(resolved)),
        unresolved=tuple(sorted(unresolved)),
    )
