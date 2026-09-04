"""Read-only views over a running witness, for the control plane.

The control-plane process must never stall or corrupt the witness (@c7v3kp), so it opens the
LMDB strictly read-only and opens it per request — no long-lived read transactions, because a
registered reader that lingers holds back the writer\'s page reclamation and grows the database
against keripy\'s 100 MB map ceiling. That is the flip side of getting the locking right, and it
is why every method here closes what it opens.

Three sources feed the surface. The LMDB supplies anything durable: identity, escrow depth,
database size, controller key state. The telemetry segment (@vxt7feoi) supplies what only the
witness\'s own loop can know: whether it is still ticking, and where its time goes. And /proc
supplies process vitals, reachable only because @a24p3kbw made the two processes co-resident.
"""

from __future__ import annotations

import os
import time

import keri
from keri.core import coring
from keri.db import basing

import witness as _witness_package

from . import vitals
from .errors import (
    ControllerUnknown,
    DbUnavailable,
    ForeignKeystore,
    WitnessError,
    WitnessNotIncepted,
)
from .telemetry import SegmentReader

_DB_FILE = "data.mdb"

#: How long a doer must hold the loop before the witness is called degraded.
#:
#: `current_doer` being set does NOT mean the loop is stuck — on a healthy witness the loop is
#: inside *some* doer most of the time, and sampling catches it there. Measured against a live
#: witness: a normal pass spends microseconds per doer, and an earlier version of this check that
#: treated any non-null `current_doer` as a wedge reported "stuck inside Directant" on a witness
#: that was perfectly fine. What distinguishes a wedge is DURATION, so that is what is measured.
#: Five seconds is ~160 tocks at hio's default 0.03125s, far outside anything normal.
_WEDGE_SECONDS = 5.0

#: Escrow stores whose depth is worth publishing, and what each one parks.
#:
#: ``qnfs`` leads because infra asked for it by name: keripy re-walks the whole query-not-found
#: escrow on EVERY hio loop pass, so its depth is the one number that turns a flood of queries for
#: AIDs this witness does not hold from a code reading into an observation. Entries age out after
#: TimeoutQNF (300s), so a rising gauge that does not fall is the signal.
_ESCROWS = {
    "query_not_found": "qnfs",
    "out_of_order": "ooes",
    "partially_signed": "pses",
    "partially_witnessed": "pwes",
    "unverified_receipt": "ures",
    "unverified_witness_receipt": "uwes",
    "unverified_validator_receipt": "vres",
    "duplicitous": "pdes",
    "misfit": "misfits",
    "delegable": "delegables",
}


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

    def _telemetry(self):
        """The telemetry segment reader, or None when this control plane serves no telemetry."""
        if self._config.telemetry_path is None:
            return None
        return SegmentReader(self._config.telemetry_path)

    def health(self, now=None) -> dict:
        """Liveness, and specifically whether the witness is still doing its job.

        The database opening is necessary and nowhere near sufficient: a witness whose hio loop
        has wedged still has a perfectly openable database, and that is the failure mode that has
        actually happened. So this also asks the telemetry segment how long the loop has been
        inside its current doer. ops.md §7 asks a probe to prove the service is working rather
        than that a port is open; for a witness, this is what that means.

        The comparison is between two readings of CLOCK_MONOTONIC taken in different processes,
        which is sound precisely because @a24p3kbw co-located them: one kernel, one clock.
        """
        rdb = self._open()
        rdb.close()
        try:
            reader = self._telemetry()
            loop = None if reader is None else reader.read()
        except WitnessError:
            # Unreadable telemetry is not itself unhealthy. A witness started from stock
            # `kli witness start` publishes none, and calling that unhealthy would make this a
            # check on our own launcher rather than on the witness.
            loop = None
        if loop is not None and loop["current_doer"] is not None:
            held = (time.monotonic() if now is None else now) - loop["current_since"]
            if held > _WEDGE_SECONDS:
                return {
                    "status": "degraded",
                    "reason": (
                        f"the loop has been inside {loop['current_doer']} for "
                        f"{held:.1f}s"
                    ),
                    "ticks": loop["ticks"],
                }
        return {"status": "ok", "ticks": None if loop is None else loop["ticks"]}

    def loop(self) -> dict:
        """The witness's hio loop: how far behind it runs, and where its time goes (@vxt7feoi)."""
        reader = self._telemetry()
        if reader is None:
            raise DbUnavailable(
                "This control plane was not told where the witness publishes its telemetry."
            )
        try:
            return reader.read()
        finally:
            reader.close()

    def version(self) -> dict:
        """What is running: this package, and the keripy it wraps."""
        return {"witness": _witness_package.__version__, "keripy": keri.__version__}

    def escrow(self) -> dict:
        """Escrow depth per store, which is where a witness under load shows it first."""
        rdb = self._open()
        try:
            depths = {}
            # Counted from each sub-database's own statistics rather than by iterating it. Three
            # reasons: it is O(1) instead of O(entries), which matters precisely when an escrow is
            # deep and something is wrong; it needs no per-class knowledge, where keripy's escrow
            # stores are six different Suber subclasses whose iteration signatures disagree; and
            # one read transaction covers every store, so the numbers are a coherent snapshot
            # rather than a series of readings taken at different moments.
            with rdb.env.begin() as txn:
                for label, attribute in _ESCROWS.items():
                    store = getattr(rdb, attribute, None)
                    if store is None:
                        continue  # an upstream bump moved it; report the rest rather than nothing
                    depths[label] = txn.stat(store.sdb)["entries"]
            return {"depths": depths, "total": sum(depths.values())}
        finally:
            rdb.close()

    def database(self) -> dict:
        """Size against keripy's fixed map ceiling — the wall a busy witness eventually hits."""
        rdb = self._open()
        try:
            info = rdb.env.info()
            used = info["last_pgno"] * rdb.env.stat()["psize"]
            return {
                "path": rdb.path,
                "used_bytes": used,
                "map_bytes": info["map_size"],
                "used_fraction": used / info["map_size"],
                "last_transaction": info["last_txnid"],
                "readers": info["num_readers"],
            }
        finally:
            rdb.close()

    def process(self) -> dict:
        """Vitals for the co-located runner process."""
        return vitals.runner_vitals()

    def controllers(self) -> dict:
        """Every controller whose key state this witness holds."""
        rdb = self._open()
        try:
            return {
                "controllers": [
                    {"aid": aid, "sequence_number": int(state.s, 16), "said": state.d}
                    for aid, state in _key_states(rdb)
                ]
            }
        finally:
            rdb.close()

    def controller(self, aid) -> dict:
        """One controller's current key state, or a 404 if this witness does not hold it."""
        rdb = self._open()
        try:
            for held, state in _key_states(rdb):
                if held == aid:
                    return {
                        "aid": held,
                        "sequence_number": int(state.s, 16),
                        "said": state.d,
                        "witnesses": list(state.b),
                        "threshold": state.bt,
                    }
            raise ControllerUnknown(
                f"This witness holds no key state for {aid}; it may not witness that controller."
            )
        finally:
            rdb.close()

    def identity(self) -> dict:
        """The witness's own AID and alias."""
        rdb = self._open()
        try:
            habs = list(rdb.habs.getTopItemIter())
            hab = _select_witness_hab(habs)
            if hab is None:
                # ~2lmg: these look alike and behave oppositely. No habs at all means the witness
                # has simply not incepted yet and the caller should wait. Habs present but none
                # of them a witness means the configured keystore belongs to somebody else, and
                # waiting will never fix it.
                if not habs:
                    raise WitnessNotIncepted(
                        "The witness database holds no identities yet; it has not been incepted."
                    )
                raise ForeignKeystore(
                    "The configured keystore holds identities, but none of them is a witness's, "
                    "so this keystore belongs to some other controller."
                )
            return {"aid": hab.hid, "alias": hab.name}
        finally:
            rdb.close()


def _key_states(rdb):
    """(aid, key-state-record) for every controller this witness holds state for."""
    for keys, state in rdb.states.getTopItemIter():
        yield (keys[0] if isinstance(keys, tuple) else keys), state
