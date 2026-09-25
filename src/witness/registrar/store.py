"""The Registrar's durable state: heads, subscriptions, batch numbers and its key (@oxtmbdfq)."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import fiki

from ..errors import (RegistrarFork, RegistrarFull, RegistrarInstance, RegistrarNoSubscription,
                      RegistrarQuota, RegistrarReplay, RegistrarStale)


@dataclass(frozen=True)
class Sighting:
    """One signed request to remember, and the bounds on how many may be remembered."""

    created: int
    signature: bytes
    now: int
    keep: int
    per_signer: int
    total: int


@dataclass(frozen=True)
class Batch:
    """What one subscriber is owed for one window."""

    number: int
    full: bool
    heads: tuple[dict, ...]


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS heads (registry TEXT PRIMARY KEY, issuer TEXT NOT NULL, "
    "digest TEXT NOT NULL, chain BLOB NOT NULL, kel BLOB NOT NULL, changed INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS subscribers (aid TEXT PRIMARY KEY, callback TEXT NOT NULL, "
    "number INTEGER NOT NULL, seen INTEGER NOT NULL, full INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value BLOB NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sightings (aid TEXT NOT NULL, created INTEGER NOT NULL, "
    "signature BLOB NOT NULL, PRIMARY KEY (aid, created, signature))",
)


class RegistrarStore:
    """One SQLite file, owned by one Registrar process at a time.

    The database file itself, resolved through any symlink, carries an exclusive lock for the
    store's lifetime, so a second Registrar reaching the same file by any path refuses to start
    rather than racing the first. Within the process, a thread lock
    serializes use of the one connection, and every read-modify-write runs inside BEGIN
    IMMEDIATE, so the read and the write it depends on see the same state.

    The directory and file are owner-only: the store holds the Registrar's private signing seed,
    and anyone who can read it can forge every batch.
    """

    def __init__(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        real = Path(os.path.realpath(path))
        previous = os.umask(0o077)
        try:
            # flock is per open file, and SQLite's own locks are POSIX fcntl locks on the same
            # file, so the two do not interfere. Opening creates an empty file SQLite accepts.
            self._lockfile = open(real, "ab")  # noqa: SIM115 - held open for the lifetime
            try:
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lockfile.close()
                raise RegistrarInstance(f"Another Registrar already holds {real}.",
                                        args=[str(real)]) from None
            self._db = sqlite3.connect(real, check_same_thread=False, isolation_level=None,
                                       timeout=10)
        finally:
            os.umask(previous)
        os.chmod(real, 0o600)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA synchronous=FULL")
        with self._tx():
            for statement in _SCHEMA:
                self._db.execute(statement)
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('change', 0)")
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('seed', ?)",
                             (fiki.Key.generate().seed,))

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()
        self._lockfile.close()

    def key(self) -> fiki.Key:
        """The Registrar's own signing key, created once and kept, since Observers pin its AID."""
        with self._lock:
            seed, = self._db.execute("SELECT value FROM meta WHERE name='seed'").fetchone()
        return fiki.Key.from_seed(bytes(seed))

    def _change(self) -> int:
        return self._db.execute("SELECT value FROM meta WHERE name='change'").fetchone()[0]

    def publish(self, *, registry: str, issuer: str, digest: str, chain: bytes,
                kel: bytes) -> str:
        """Keep a snapshot only if its chain extends the head held; return the head's digest.

        An exact repeat is acknowledged again, because a publisher retries until it gets an exact
        acknowledgement. A strict prefix is stale and anything else forks (@5m2m4ozz).
        """
        with self._tx():
            held = self._db.execute("SELECT digest, chain FROM heads WHERE registry=?",
                                    (registry,)).fetchone()
            if held is not None:
                held_digest, held_chain = held[0], bytes(held[1])
                if chain == held_chain:
                    return held_digest
                if held_chain.startswith(chain):
                    raise RegistrarStale(f"The snapshot for {registry} is older than its head.",
                                         args=[registry])
                if not chain.startswith(held_chain):
                    raise RegistrarFork(f"The snapshot for {registry} does not extend its head.",
                                        args=[registry])
            change = self._change() + 1
            self._db.execute("UPDATE meta SET value=? WHERE name='change'", (change,))
            self._db.execute("INSERT OR REPLACE INTO heads VALUES (?, ?, ?, ?, ?, ?)",
                             (registry, issuer, digest, chain, kel, change))
        return digest

    def head(self, registry: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT registry, issuer, digest, chain, kel FROM heads "
                                   "WHERE registry=?", (registry,)).fetchone()
        return None if row is None else _head(row)

    def _record(self, aid: str, sighting: Sighting | None) -> None:
        """Inside a transaction: refuse a replay or an over-quota request, else remember it.

        Called in the same transaction as the action it protects, so a request that is refused
        for any reason leaves no sighting behind and can be retried.
        """
        if sighting is None:
            return
        self._db.execute("DELETE FROM sightings WHERE created < ?",
                         (sighting.now - sighting.keep,))
        if self._db.execute("SELECT 1 FROM sightings WHERE aid=? AND created=? AND signature=?",
                            (aid, sighting.created, sighting.signature)).fetchone():
            raise RegistrarReplay("That signed request was already acted on.", args=[aid])
        mine, = self._db.execute("SELECT COUNT(*) FROM sightings WHERE aid=?",
                                 (aid,)).fetchone()
        everyone, = self._db.execute("SELECT COUNT(*) FROM sightings").fetchone()
        if mine >= sighting.per_signer or everyone >= sighting.total:
            raise RegistrarQuota("Too many recent signed requests are remembered already.",
                                 args=[mine, everyone])
        self._db.execute("INSERT INTO sightings VALUES (?, ?, ?)",
                         (aid, sighting.created, sighting.signature))

    def subscribe(self, aid: str, callback: str, *, limit: int | None = None,
                  sighting: Sighting | None = None) -> None:
        """Subscribe ``aid``, or update its callback without disturbing its sequence.

        A new subscription starts at number 1 with a full batch. Repeating a subscription, or
        moving it to a new callback, continues the sequence, so a replayed or retried request
        cannot make a subscriber's numbering go backwards. A subscription's identity is its AID,
        so each AID has at most one; ``limit`` caps how many AIDs may subscribe at all.
        """
        with self._tx():
            self._record(aid, sighting)
            if self._db.execute("SELECT 1 FROM subscribers WHERE aid=?", (aid,)).fetchone():
                self._db.execute("UPDATE subscribers SET callback=? WHERE aid=?", (callback, aid))
                return
            count, = self._db.execute("SELECT COUNT(*) FROM subscribers").fetchone()
            if limit is not None and count >= limit:
                raise RegistrarFull(f"This Registrar already has {count} subscriptions, its "
                                    "limit.", args=[count])
            self._db.execute("INSERT INTO subscribers VALUES (?, ?, 0, 0, 1)", (aid, callback))

    def unsubscribe(self, aid: str, *, sighting: Sighting | None = None) -> bool:
        """End ``aid``'s subscription. With a ``sighting``, a missing subscription is refused
        inside the transaction, so the request leaves nothing behind."""
        with self._tx():
            self._record(aid, sighting)
            ended = self._db.execute("DELETE FROM subscribers WHERE aid=?", (aid,)).rowcount > 0
            if sighting is not None and not ended:
                raise RegistrarNoSubscription(f"{aid} has no subscription here.", args=[aid])
            return ended

    def subscribers(self) -> list[tuple[str, str]]:
        with self._lock:
            return [tuple(row) for row in self._db.execute(
                "SELECT aid, callback FROM subscribers ORDER BY aid")]

    def first_sighting(self, aid: str, created: int, signature: bytes, *, now: int,
                       keep: int) -> bool:
        """Remember a signed request on its own; False if it is a replay still in its window.

        A sighting is kept for ``keep`` seconds after ``created``, the verifier's whole
        acceptance window, and expired ones are pruned first. The routes use ``_record`` inside
        the action's own transaction instead; this is the same judgment, unbounded, for callers
        with no action to tie it to.
        """
        try:
            with self._tx():
                self._record(aid, Sighting(created, signature, now, keep,
                                           per_signer=2**31, total=2**31))
        except RegistrarReplay:
            return False
        return True

    def sighting_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]

    def compose(self, aid: str) -> Batch:
        """The next batch for ``aid``: every head if its subscription is new, else the changes.

        The number is consumed here, whether or not delivery succeeds, so an Observer that misses
        a batch sees a gap rather than a silently skipped window (@3m2eys6w).
        """
        with self._tx():
            row = self._db.execute("SELECT number, seen, full FROM subscribers WHERE aid=?",
                                   (aid,)).fetchone()
            if row is None:
                raise RegistrarNoSubscription(f"{aid} has no subscription here.", args=[aid])
            number, seen, full = row
            change = self._change()
            rows = self._db.execute(
                "SELECT registry, issuer, digest, chain, kel FROM heads"
                + ("" if full else " WHERE changed > ?") + " ORDER BY registry",
                () if full else (seen,)).fetchall()
            self._db.execute("UPDATE subscribers SET number=?, seen=?, full=0 WHERE aid=?",
                             (number + 1, change, aid))
        return Batch(number + 1, bool(full), tuple(_head(row) for row in rows))


def _head(row) -> dict:
    registry, issuer, digest, chain, kel = row
    return {"registry": registry, "issuer": issuer, "digest": digest,
            "chain": bytes(chain), "kel": bytes(kel)}
