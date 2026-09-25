"""The Registrar's durable state: heads, subscriptions, batch numbers and its key (@oxtmbdfq)."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import fiki

from ..errors import RegistrarFork, RegistrarNoSubscription, RegistrarStale


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
)


class RegistrarStore:
    """One SQLite file, one lock: waitress answers requests on several threads at once."""

    def __init__(self, path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA synchronous=FULL")
            for statement in _SCHEMA:
                self._db.execute(statement)
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('change', 0)")
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('seed', ?)",
                             (fiki.Key.generate().seed,))

    def close(self) -> None:
        self._db.close()

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
        with self._lock, self._db:
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

    def subscribe(self, aid: str, callback: str) -> None:
        """Start (or restart) a subscription: numbering from 1, beginning with a full batch."""
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO subscribers VALUES (?, ?, 0, 0, 1)",
                             (aid, callback))

    def unsubscribe(self, aid: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM subscribers WHERE aid=?", (aid,)).rowcount > 0

    def subscribers(self) -> list[tuple[str, str]]:
        with self._lock:
            return [tuple(row) for row in self._db.execute(
                "SELECT aid, callback FROM subscribers ORDER BY aid")]

    def compose(self, aid: str) -> Batch:
        """The next batch for ``aid``: every head if its subscription is new, else the changes.

        The number is consumed here, whether or not delivery succeeds, so an Observer that misses
        a batch sees a gap rather than a silently skipped window (@3m2eys6w).
        """
        with self._lock, self._db:
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
