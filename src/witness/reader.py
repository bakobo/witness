"""Read-only reader over a witness's LMDB.

The control-plane process must never stall or corrupt the witness, so it opens the witness's
LMDB strictly read-only, and opens it per request (no long-lived read transactions). The reader
serves liveness (:meth:`WitnessReader.health`) and identity (:meth:`WitnessReader.info`).
"""

from __future__ import annotations

import os

import keri
from keri.core import coring
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


def _is_witness_hab(hr):
    """Whether ``hr`` can be this witness's own identity.

    Two things disqualify a hab. A group hab is a multisig identity rather than a single
    controller's, and carries a non-``None`` ``mid``. A **transferable** hab cannot be a witness
    at all: a transferable AID's key state is established by its own witnesses, so a transferable
    witness would depend on witnesses of its own and the definition would not terminate. Every
    witness AID is therefore non-transferable, and the prefix says so.
    """
    if hr.mid is not None:
        return False
    return not coring.Prefixer(qb64=hr.hid).transferable


def _select_witness_hab(items):
    """Return the first hab record from ``items`` that could be this witness's own identity, or
    ``None`` if there is none.

    ``items`` yields ``(keys, habitat_record)`` pairs. Selecting nothing is the honest answer for
    a keystore that belongs to somebody else: reporting another controller's AID as this
    witness's identity is worse than reporting no identity at all.
    """
    for _keys, hr in items:
        if _is_witness_hab(hr):
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
            # Opened in two steps deliberately. `Baser(reopen=True, readonly=True)` looks correct
            # and silently opens the environment read-WRITE: LMDBer.__init__ consumes `readonly`
            # and sets self.readonly, then Filer.__init__ calls reopen() without it, and
            # LMDBer.reopen's `readonly=False` default overwrites what was just set — its
            # `if readonly is not None` guard can never be False. Passing the flag to reopen()
            # directly is the only way it reaches lmdb.open. @k3p7wr's "physically cannot corrupt"
            # is a property of the environment, not of our restraint, so it has to be real; ~5s3e
            # is the same defect seen from the other side.
            rdb = basing.Baser(
                name=self._config.name,
                base=self._config.base,
                temp=False,
                headDirPath=self._config.head_dir_path,
                reopen=False,
            )
            rdb.reopen(readonly=True)
            return rdb
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
                    "The witness database opened but holds no witness identity."
                )
            return {
                "aid": hab.hid,
                "alias": hab.name,
                "keripy_version": keri.__version__,
                "db_path": rdb.path,
            }
        finally:
            rdb.close()
