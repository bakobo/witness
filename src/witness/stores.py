"""How big every keripy store a witness process opens is mapped (@8mzxgpds).

keripy maps each LMDB store at 100 MiB, and a witness keeps every event of every controller it
witnesses: keri-bench filled two of three witnesses at 40,000 credentials of one issuer, and the
issuer stalled at toad 2. Each process this CLI starts owns every keripy store it opens, so the
size is set on keripy's own class, its configuration idiom, before any store opens.
"""

from __future__ import annotations

import os

from keri.db import basing, dbing

from .errors import InvalidArguments

MAP_SIZE_ENV = "WITNESS_LMDB_MAP_SIZE"
"""An environment variable because the image's command is fixed, as with WITNESS_OPERATORS."""

DEFAULT_MAP_SIZE = 16 * 1024 ** 3
"""16 GiB, heti's choice for an issuer's stores. On Linux a map is reserved address space and the
file grows only as pages are written, so an idle witness pays nothing for it."""

_LARGEST = 2 ** 63 - 1
"""The largest map py-lmdb accepts; a larger one overflows before LMDB sees it."""

_DIGITS = len(str(_LARGEST))
"""So a longer value is refused here, by length, rather than converted first."""


def map_size(environ=os.environ) -> int:
    """The map size this process gives every keripy store, refusing one that is not a size."""
    raw = environ.get(MAP_SIZE_ENV)
    if raw is None:
        return DEFAULT_MAP_SIZE
    text = raw.strip()
    if (len(text) > _DIGITS or not text.isascii() or not text.isdigit()
            or not 0 < int(text) <= _LARGEST):
        shown = raw if len(raw) <= 40 else raw[:40] + "…"
        raise InvalidArguments(
            f"{MAP_SIZE_ENV} is {shown!r}, which is not a byte count. Set it to a whole number of "
            f"bytes from 1 to {_LARGEST}, or unset it for {DEFAULT_MAP_SIZE} (16 GiB).",
            args=[MAP_SIZE_ENV],
        )
    return int(text)


def apply(size: int) -> None:
    """Map every keripy store this process opens from now on at ``size`` bytes.

    keripy's Baser reads its own KERI_BASER_MAP_SIZE after the class, so that is set too, or a
    stale smaller value would shrink the database alone. A store that filled at a smaller map
    opens at this one, since LMDB takes the requested size whenever it holds the data.
    """
    dbing.LMDBer.MapSize = size
    os.environ[basing.KERIBaserMapSizeKey] = str(size)
