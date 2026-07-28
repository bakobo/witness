"""Read-only reader over a witness's LMDB.

The control-plane process must never stall or corrupt the witness, so it opens the witness's
LMDB strictly read-only, and opens it per request (no long-lived read transactions). The reader
serves liveness (:meth:`WitnessReader.health`) and identity (:meth:`WitnessReader.info`).
"""

from __future__ import annotations

import os

import keri
from keri.db import basing

from .errors import DbUnavailable, IdentityUnavailable

_DB_FILE = "data.mdb"


def _db_dir_candidates(config):
    """The directories keripy could resolve the witness DB to for ``config``, computed without
    opening or creating anything.

    Mirrors hio's Filer resolution: with an explicit ``head_dir_path`` the DB lives only under
    that head; with the default (``None``) keripy tries its primary head and falls back to the
    alt (home) head, so we check both.
    """
    if config.head_dir_path is not None:
        heads = [(config.head_dir_path, basing.Baser.TailDirPath)]
    else:
        heads = [
            (basing.Baser.HeadDirPath, basing.Baser.TailDirPath),
            (basing.Baser.AltHeadDirPath, basing.Baser.AltTailDirPath),
        ]
    return [
        os.path.abspath(os.path.expanduser(os.path.join(head, tail, config.base, config.name)))
        for head, tail in heads
    ]


def _resolve_existing_db(config):  # ~5s3e — keripy readonly open creates a phantom env on a missing DB
    """Return the witness DB directory that actually holds an LMDB file, or ``None``.

    A read-only ``Baser`` open of a *missing* DB does not fail — keripy creates an empty env
    (polluting the path, or ``~/.keri`` under the default head). Guarding on the real ``data.mdb``
    keeps the reader honest (a missing witness reports unavailable) and truly read-only (we never
    create a phantom DB).
    """
    for path in _db_dir_candidates(config):
        if os.path.exists(os.path.join(path, _DB_FILE)):
            return path
    return None


def _select_witness_hab(items):
    """Return the first local (non-group) hab record from ``items``, or ``None`` if there is none.

    ``items`` yields ``(keys, habitat_record)`` pairs; a group hab has a non-``None`` ``mid``.
    """
    for _keys, hr in items:
        if hr.mid is None:
            return hr
    return None


class WitnessReader:
    """Opens the witness LMDB read-only to answer liveness and identity queries."""

    def __init__(self, config):
        self._config = config

    def _open(self):
        """Open the witness database read-only, or raise DbUnavailable if it cannot be read."""
        if _resolve_existing_db(self._config) is None:
            raise DbUnavailable(
                "The witness database was not found at the configured location; the witness may "
                "not be running yet."
            )
        try:
            return basing.Baser(
                name=self._config.name,
                base=self._config.base,
                temp=False,
                headDirPath=self._config.head_dir_path,
                reopen=True,
                readonly=True,
            )
        except Exception as exc:
            raise DbUnavailable(
                "The witness database could not be opened for reading; the witness may not be "
                "running yet."
            ) from exc

    def health(self) -> dict:
        """Liveness: confirm the witness database opens read-only."""
        rdb = self._open()
        rdb.close()
        return {"status": "ok"}

    def info(self) -> dict:
        """Identity: report the witness's own AID, alias, keripy version, and database path."""
        rdb = self._open()
        try:
            hab = _select_witness_hab(rdb.habs.getTopItemIter())
            if hab is None:
                raise IdentityUnavailable(
                    "The witness database opened but has no witness identity yet."
                )
            return {
                "aid": hab.hid,
                "alias": hab.name,
                "keripy_version": keri.__version__,
                "db_path": rdb.path,
            }
        finally:
            rdb.close()
