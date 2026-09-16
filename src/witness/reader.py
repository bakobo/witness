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
from keri import kering
from keri.core import coring
from keri.db import basing

import witness as _witness_package

from . import decls as _decls, paths, vitals
from .errors import (
    ControllerUnknown,
    DatabaseTooNew,
    DbUnavailable,
    ForeignKeystore,
    MigrationRequired,
    TelemetryNotConfigured,
    WitnessError,
    WitnessNotIncepted,
)
from .telemetry import SegmentReader

#: How many delegation levels to follow before giving up and reporting the rest unfollowed.
#:
#: A guard rather than an opinion about real chains, which are short. KERI should not permit a
#: delegation cycle, but this walks a database rather than a proof, and a malformed store must not
#: be able to spin the request forever. A `seen` set catches an actual cycle; this catches a chain
#: long enough that answering it would cost more than the answer is worth.
_MAX_DELEGATION_DEPTH = 8

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


def _resolve_existing_db(config):  # ~5s3e — keripy readonly open creates a phantom env on a missing DB
    """The witness DB directory that actually holds a database, or ``None``.

    Delegated to :mod:`witness.paths`, which resolves every store from its own keripy class
    constants rather than from an assumed layout — see the note there about the alt head using a
    dotted tail, which a hand-computed "keri home" gets wrong.
    """
    return paths.resolve(basing.Baser, config)


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
        # Opened in two steps deliberately. `Baser(reopen=True, readonly=True)` looks correct and
        # silently opens the environment read-WRITE: LMDBer.__init__ consumes `readonly` and sets
        # self.readonly, then Filer.__init__ calls reopen() without it, and LMDBer.reopen's
        # `readonly=False` default overwrites what was just set — its `if readonly is not None`
        # guard can never be False. Passing the flag to reopen() directly is the only way it
        # reaches lmdb.open. @k3p7wr's "physically cannot corrupt" is a property of the
        # environment, not of our restraint, so it has to be real; ~5s3e is the same defect seen
        # from the other side.
        rdb = basing.Baser(
            name=self._config.name,
            base=self._config.base,
            temp=False,
            headDirPath=self._config.head_dir_path,
            reopen=False,
        )
        try:
            rdb.reopen(readonly=True)
        except Exception as exc:
            # Close before re-raising. keripy validates the schema AFTER lmdb.open has succeeded,
            # so an environment we never closed stays registered in py-lmdb's per-process table
            # and every later open fails with "already open in this process" instead of the real
            # reason. The control plane opens per request, so without this the true cause was
            # visible only in the very first request anyone made (~7hrf).
            rdb.close()
            raise _classify_open_failure(exc) from exc
        return rdb

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
            raise TelemetryNotConfigured(
                "This control plane was started with --no-telemetry, so there is no loop "
                "telemetry to report; the witness database is unaffected."
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

    def tags(self) -> dict:
        """What this witness says about itself (@vqqh6zdk).

        Answers from configuration alone, deliberately touching no database. The question an
        operator asks during a bad deploy is "is this the laboratory box?", and a witness whose
        database will not open is exactly when they ask it — so making this endpoint depend on the
        database would make it unavailable precisely when it is wanted.

        ``source`` is the seam @nlunqygr named, and it now carries two values. A signed
        declaration published by this witness wins over operator configuration, because it is the
        thing a third party can actually verify; configuration is what a witness has to say for
        itself before it has said anything on the wire.
        """
        tags, source = self._effective_tags()
        return {"tags": list(tags), "source": source}

    def attribs(self) -> dict:
        """What this witness publishes for a person to read, rather than for software to act on.

        Kept a separate noun from :meth:`tags` rather than folded into one document (@e4ceoopg).
        The signed reply routes of the second increment want to be separate, because BADA orders
        each route independently: one route would mean re-signing and re-timestamping the testnet
        assertion in order to correct a typo in a contact address.

        Nothing here is inherited by an AID. Union is defined for tag names and undefined for
        key/value pairs — three witnesses reporting three regions have no natural merge — and no
        attribute carries the against-interest property that makes `testnet` worth believing.
        """
        record = self._declared("attribs")
        if record is not None:
            return {"attribs": dict(record.attribs), "source": "signed-reply"}
        if self._config.attribs:
            return {"attribs": dict(self._config.attribs), "source": "operator-config"}
        seeded = self._seeded()
        if seeded is not None and seeded.attribs:
            return {"attribs": dict(seeded.attribs), "source": "seed-file"}
        return {"attribs": {}, "source": "operator-config"}

    def _seeded(self):
        """Declarations from the seed file a pool writes into the volume, or None (@hjz7b7qo).

        Fails closed on the value and open on the service: an absent, unreadable or malformed file
        means this witness declares nothing, never that the control plane refuses to answer. A
        typo in a provisioning script that presented as a dead control plane would be a worse
        failure than one that presented as a witness with no declarations.
        """
        path = self._config.decl_file
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(_decls.MAX_SEED_BYTES + 1)
        except (OSError, UnicodeError):
            # UnicodeError as well as OSError: bytes that are not UTF-8 raise during read, and a
            # UnicodeDecodeError is a ValueError rather than an OSError, so it would otherwise
            # escape and turn a malformed file into a 500 — the opposite of what this promises.
            return None
        try:
            return _decls.from_seed(text)
        except WitnessError:
            return None

    def _effective_tags(self, rdb=None):
        """The tags this witness effectively claims, and where they came from.

        One place, because the alternative is what shipped in an earlier draft of this branch:
        /v1/witness/tags applied the full precedence while controller/{aid} read the flag alone,
        so a pooled witness declared `testnet` on one endpoint and the AIDs it witnessed inherited
        nothing on the other. Two readings of the same question have to come from one function.

        ``rdb`` lets a caller that already has the database open lend it, so answering inside
        controller() costs no second open.
        """
        record = self._declared("tags", rdb=rdb)
        if record is not None:
            return tuple(record.tags), "signed-reply"
        if self._config.tags:
            return tuple(self._config.tags), "operator-config"
        seeded = self._seeded()
        if seeded is not None and seeded.tags:
            return tuple(seeded.tags), "seed-file"
        return (), "operator-config"

    def _declared(self, kind, rdb=None):
        """This witness's own signed declaration of ``kind``, or None if there is not one.

        Four different absences collapse to None on purpose, because they call for the same
        answer. The database may not open, which is the case @nlunqygr cares about most: an
        operator asks "is this the laboratory box?" precisely when things are broken, so a
        declaration endpoint that fails with the database would be missing exactly when wanted.
        The installed keripy may predate the decl reply routes, and it does today — read through
        ``getattr`` for the same reason escrow() does, rather than assuming a store exists. The
        keystore may not identify a witness. And the witness may simply not have declared yet.

        None of the four is an error. A witness whose declarations are still operator
        configuration is in a normal state, not a degraded one.
        """
        if rdb is not None:
            return _declared_in(rdb, kind)
        try:
            opened = self._open()
        except WitnessError:
            return None
        try:
            return _declared_in(opened, kind)
        finally:
            opened.close()

    def _own_tags(self, rdb):
        """``{our own witness AID: the tags we effectively claim}``, or empty if we cannot say.

        Empty rather than raising. This feeds a member added to an endpoint that already worked,
        and a keystore we cannot make sense of should cost the caller the tag derivation, not the
        key state they actually asked for.
        """
        hab = _select_witness_hab(rdb.habs.getTopItemIter())
        if hab is None:
            return {}
        tags, _source = self._effective_tags(rdb=rdb)
        return {hab.hid: tags}

    def controller(self, aid) -> dict:
        """One controller's current key state, or a 404 if this witness does not hold it."""
        rdb = self._open()
        try:
            for held, state in _key_states(rdb):
                if held == aid:
                    witnesses, unfollowed = _chain(rdb, state)
                    derived = _decls.derive(
                        witnesses=witnesses,
                        known=self._own_tags(rdb),
                        unfollowed=unfollowed,
                    )
                    return {
                        "aid": held,
                        "sequence_number": int(state.s, 16),
                        "said": state.d,
                        "witnesses": list(state.b),
                        "threshold": state.bt,
                        "tags": {
                            "derived": list(derived.tags),
                            "from": list(derived.resolved),
                            "unresolved": list(derived.unresolved),
                            "unfollowed": list(derived.unfollowed),
                        },
                    }
            raise ControllerUnknown(
                f"This witness holds no key state for {aid}; it may not witness that controller.",
                args=[aid],
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


def _chain(rdb, state):
    """Every witness reachable for this AID, and the delegators we could not follow (@4qrayq3j).

    A delegated AID's authority is rooted in its delegator, so laboratory witnesses under the
    delegator taint the delegate. The walk goes as far as this database reaches and no further:
    nothing obliges a witness to hold key state for a delegator, so stopping early is the normal
    outcome and is reported rather than papered over.
    """
    witnesses = []
    unfollowed = []
    seen = {state.i}
    current = state
    for _level in range(_MAX_DELEGATION_DEPTH):
        witnesses.extend(current.b)
        delegator = getattr(current, "di", "")
        if not delegator:
            return witnesses, unfollowed
        if delegator in seen:
            # Not reachable through valid KERI, which anchors a delegation in the delegator's own
            # KEL and so cannot close a loop. Reachable through a corrupt store, and a read-only
            # observer that hangs on one is worse than one that declines to answer the last hop.
            return witnesses, unfollowed
        seen.add(delegator)
        upper = rdb.states.get(keys=delegator)
        if upper is None:
            unfollowed.append(delegator)
            return witnesses, unfollowed
        current = upper
    # Depth exhausted. `current` was fetched on the last pass and its witnesses were never
    # collected, so IT is the first level we failed to account for — naming its delegator instead
    # would skip a whole witness set while claiming to say where we stopped.
    unfollowed.append(current.i)
    return witnesses, unfollowed


def _declared_in(rdb, kind):
    """Read one signed declaration out of an already-open database, or None.

    Read through ``getattr`` for the same reason escrow() does — a keripy without the decl reply
    routes has no such store, and assuming one would fail rather than fall back.
    """
    store = getattr(rdb, "decls", None)  # ~4ky2
    if store is None:
        return None
    hab = _select_witness_hab(rdb.habs.getTopItemIter())
    if hab is None:
        return None
    return store.get(keys=(hab.hid, kind))


def _classify_open_failure(exc):
    """Turn keripy's open-time refusal into an error that names the operator's next move.

    keripy distinguishes the two upgrade directions by exception TYPE, which is what this matches
    on rather than message text: ``DatabaseError`` from ``Baser.reload`` when the database is
    behind the library and migrations are outstanding, and ``ConfigurationError`` when it is
    ahead. tests/test_keripy_contract.py pins both, so an upstream change that reshuffles them
    fails loudly here rather than silently collapsing back to "the witness may not be running".
    """
    if isinstance(exc, kering.ConfigurationError):
        return DatabaseTooNew(
            "This witness database was written by a newer keripy than this build carries, so it "
            "cannot be opened. Deploy the newer image again, or roll the volume back by "
            "restoring the backup taken before the upgrade."
        )
    if isinstance(exc, kering.DatabaseError):
        return MigrationRequired(
            "This witness database is behind the keripy this build carries and will not open "
            "until its migrations have run. Run `kli migrate run --name <keystore>` against the "
            "volume, then start the witness again."
        )
    return DbUnavailable(
        "The witness database could not be opened for reading; the witness may not be running yet."
    )


def _key_states(rdb):
    """(aid, key-state-record) for every controller this witness holds state for.

    ``db.states`` is a Komer keyed by AID, and keripy yields its keys as a one-element tuple —
    verified against the pinned commit, and asserted in tests/test_keripy_contract.py so an
    upstream change to that shape fails loudly here rather than silently yielding tuples where
    AIDs are expected.
    """
    for keys, state in rdb.states.getTopItemIter():
        yield keys[0], state
