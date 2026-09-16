"""What a witness says about itself, and what an AID inherits from its witnesses (@vqqh6zdk).

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

:func:`from_operator` is a door in the sense of dev/standards/input-handling.md: size, then shape,
then meaning, each only trustworthy if the one before it ran. Its peer counterpart arrives with the
signed reply route, where an unknown bare name means a newer vocabulary rather than a typo and must
pass through opaquely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import InvalidArguments

#: The one standard tag. Its meaning is COIA APPENDIX-D flag 6's, deliberately verbatim in sense:
#: an experimental, test or demonstration environment with no real-world consequence.
TESTNET = "testnet"

KNOWN = {
    TESTNET: (
        "This witness belongs to an experimental, test or demonstration environment and carries "
        "no real-world consequence to reputation, governance or cost."
    ),
}

#: A flood guard, not an opinion about how many tags a witness legitimately has. Sixteen is far
#: past any real configuration and far below anything that would trouble the process.
MAX_TAGS = 16
#: Likewise a guard. A name long enough to need 64 characters is not communicating.
MAX_TAG_LENGTH = 64

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


def _admit(value):
    """Bound one tag by size, then shape, then meaning, or refuse it by name."""
    if not isinstance(value, str):
        raise InvalidArguments("Every tag must be text, but one of the supplied tags was not.")
    if len(value) > MAX_TAG_LENGTH:
        # The length, never the value: a refusal that echoes a 500-character argument back at the
        # operator has handed them their own payload instead of the rule (rubric item 6).
        raise InvalidArguments(
            f"A tag may be at most {MAX_TAG_LENGTH} characters, but one was {len(value)}."
        )
    if not _SHAPE.match(value):
        raise InvalidArguments(
            "A tag must be lowercase letters, digits and hyphens in dot-separated segments, each "
            "beginning with a letter, as in 'testnet' or 'bakobo.pool'."
        )
    if "." not in value and value not in KNOWN:
        # Bare names are the shared vocabulary, so an unrecognized one is a typo rather than an
        # extension — and a misspelled `testnet` that silently fails to apply leaves the witness
        # looking production-grade with nothing to say otherwise.
        raise InvalidArguments(
            f"{value!r} is not a defined tag. The defined tags are "
            f"{', '.join(sorted(KNOWN))}; a tag of your own needs a vendor prefix, as in "
            "'bakobo.pool'."
        )
    return value


def from_operator(values):
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
    return tuple(sorted({_admit(value) for value in supplied}))


def derive(witnesses, known):
    """Union the tags of every witness in ``witnesses`` whose tags ``known`` holds.

    Monotone by construction: one tagged witness is enough, and no quorum of untagged ones excuses
    it. That is the whole rule — a witness set is only as production-ready as its least
    production-ready member, and @dhh2gnvv makes the resulting marking one the consumer never
    clears.

    ``witnesses`` is an AID's witness list (``state.b``); ``known`` maps a witness AID to its tags.
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
