"""Sparse, generation-scoped change index beside the witness's keripy LMDB (@6y23f6w2)."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import fiki
from keri import kering

from .errors import ChangePageTooLarge, ChangeUnavailable, CursorConflict, ChangeInput
from .reader import _key_states

DEFAULT_BUCKET_WIDTH = 60
DEFAULT_RETENTION = 7 * 86400
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# ~5sqv: a complete KEL above this bound needs a future event-fragment cursor.
MAX_PAGE_REPLAY_BYTES = 16 * 1024 * 1024
_MAX_CURSOR = 1024
_GENUS = kering.Versionage(major=2, minor=0)


class ChangeFeed:
    """A read-only LMDB sweep with a private SQLite index and a persistent response key.

    SQLite is outside keripy's environment; one connection and a lock keep a sweep and
    concurrent WSGI requests from sharing a transaction. The connection is never lent to keripy.
    """

    def __init__(self, path, reader, *, clock=time.time, bucket_width=DEFAULT_BUCKET_WIDTH,
                 retention=DEFAULT_RETENTION):
        self.reader = reader
        self.clock = clock
        self.bucket_width = bucket_width
        self.retention = retention
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS seen (aid TEXT PRIMARY KEY, sn INTEGER "
                             "NOT NULL, said TEXT NOT NULL, fn INTEGER NOT NULL, receipts "
                             "INTEGER NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS change (bucket INTEGER NOT NULL, aid "
                             "TEXT NOT NULL, from_fn INTEGER NOT NULL, PRIMARY KEY (bucket, aid))")
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value BLOB "
                             "NOT NULL)")
            # A snapshot can restore LMDB and this SQLite file together. Only a new generation
            # on every process start makes a pre-restore cursor unambiguously stale.
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('generation', ?)",
                             (uuid.uuid4().hex,))
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('seed', ?)",
                             (fiki.Key.generate().seed,))
        self.generation = self._meta("generation")
        self._key = fiki.Key.from_seed(bytes(self._meta("seed")))

    def _meta(self, name):
        with self._lock:
            return self._db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()[0]

    def key(self):
        """The stable non-transferable key a watcher pins from /identity."""
        return self._key

    def close(self):
        self.stop()
        with self._lock:
            self._db.close()

    def start(self):
        """Sweep once before serving, then once per bucket width in this control-plane process."""
        try:
            self.sweep()
        except Exception:
            self._failed = True
        self._thread = threading.Thread(target=self._run, name="witness-change-sweep", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self):
        while not self._stop.wait(self.bucket_width):
            try:
                self.sweep()
            except Exception:
                # A failed sweep must never silently advance a cursor. page() refuses until a
                # successful sweep clears this flag; the control plane stays alive for health.
                self._failed = True

    def _bucket(self):
        return int(self.clock()) // self.bucket_width

    def _cutoff(self):
        return self._bucket() - (self.retention + self.bucket_width - 1) // self.bucket_width

    def _reset(self):
        self.generation = uuid.uuid4().hex
        self._db.execute("DELETE FROM seen")
        self._db.execute("DELETE FROM change")
        self._db.execute("UPDATE meta SET value=? WHERE name='generation'", (self.generation,))

    def sweep(self):
        """Compare every key state and latest-event receipt count without writing keripy."""
        rdb = self.reader._open()
        try:
            records = []
            for aid, state in _key_states(rdb):
                receipts = len(rdb.wigs.get(keys=(aid, state.d)) or ())
                records.append((aid, int(state.s, 16), state.d, int(state.f, 16), receipts))
        finally:
            rdb.close()
        bucket = self._bucket()
        with self._lock, self._db:
            old = {row[0]: row[1:] for row in self._db.execute(
                "SELECT aid, sn, said, fn, receipts FROM seen")}
            current = {row[0] for row in records}
            # A restore or replacement can remove an AID or roll its head back. Keeping the
            # old generation would make a watcher's cursor claim changes it never saw.
            if old.keys() - current or any(
                aid in old and (sn < old[aid][0] or fn < old[aid][2] or
                (sn == old[aid][0] and said != old[aid][1]) or
                ((sn, said, fn) == old[aid][:3] and receipts < old[aid][3]))
                for aid, sn, said, fn, receipts in records
            ):
                self._reset()
                old = {}
            for aid, sn, said, fn, receipts in records:
                previous = old.get(aid)
                if previous == (sn, said, fn, receipts):
                    continue
                if previous is None:
                    from_fn = 0
                elif (sn, said, fn) == previous[:3]:
                    from_fn = fn  # latest-event receipt arrived
                else:
                    from_fn = min(fn, previous[2] + 1)
                self._db.execute("INSERT INTO change VALUES (?, ?, ?) ON CONFLICT(bucket, aid) "
                                 "DO UPDATE SET from_fn=MIN(from_fn, excluded.from_fn)",
                                 (bucket, aid, from_fn))
                self._db.execute("INSERT INTO seen VALUES (?, ?, ?, ?, ?) ON CONFLICT(aid) DO "
                                 "UPDATE SET sn=excluded.sn, said=excluded.said, fn=excluded.fn, "
                                 "receipts=excluded.receipts", (aid, sn, said, fn, receipts))
            self._db.execute("DELETE FROM change WHERE bucket < ?", (self._cutoff(),))
        self._failed = False

    def cursor_for(self, when):
        """A cursor at a known clock instant; exposed for integration and deterministic tests."""
        return self._encode(int(when) // self.bucket_width)

    def _encode(self, bucket, last=None, answer=None):
        raw = json.dumps([self.generation, bucket, last, answer], separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def _decode(self, cursor):
        if not isinstance(cursor, str) or len(cursor) > _MAX_CURSOR or not cursor:
            raise ChangeInput("The since cursor is empty or longer than its bound.")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_",
                                   validate=True)
            values = json.loads(raw)
            if (not isinstance(values, list) or len(values) != 4 or
                not isinstance(values[0], str) or
                type(values[1]) is not int or
                (values[2] is not None and not isinstance(values[2], str)) or
                (values[3] is not None and type(values[3]) is not int)):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ChangeInput("The since cursor has no usable shape.") from None
        generation, bucket, last, answer = values
        if (bucket < -1 or bucket > self._bucket() or
            (last is None) != (answer is None) or
            (answer is not None and (answer < 0 or answer > self._bucket()))):
            raise ChangeInput("The since cursor names an impossible page position.")
        if generation != self.generation or (bucket != -1 and bucket < self._cutoff()):
            raise CursorConflict("The since cursor belongs to another generation or is past "
                                 "this witness's retention window. Start again without since.")
        return bucket, last, answer

    def page(self, since, limit=DEFAULT_LIMIT):
        """Return one AID page, replaying each from its earliest changed first-seen ordinal."""
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ChangeInput(f"The limit must be an integer from 1 through {MAX_LIMIT}.")
        if getattr(self, "_failed", False):
            raise ChangeUnavailable("The last change sweep failed; retry after it succeeds.")
        with self._lock:
            bucket, last, answer = (-1, None, None) if since is None else self._decode(since)
            if answer is None:
                answer = self._bucket()
            generation = self.generation
            try:
                if bucket == -1:
                    rows = self._db.execute("SELECT aid, 0, sn, said, fn FROM seen WHERE aid > ? ORDER BY aid "
                                            "LIMIT ?", (last or "", limit + 1)).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT change.aid, MIN(from_fn), seen.sn, seen.said, seen.fn FROM change "
                        "JOIN seen ON seen.aid = change.aid WHERE bucket >= ? AND change.aid > ? "
                        "GROUP BY change.aid ORDER BY change.aid LIMIT ?",
                        (bucket, last or "", limit + 1)
                    ).fetchall()
            except sqlite3.Error:
                raise ChangeUnavailable("The change index could not be read. Retry after its "
                                        "next successful sweep.") from None
        more = len(rows) > limit
        rows = rows[:limit]
        rdb = self.reader._open() if rows else None
        try:
            change = []
            replay_bytes = 0
            for aid, fn, seen_sn, seen_said, seen_fn in rows:
                state = rdb.states.get(keys=aid)
                if (state is None or int(state.s, 16) < seen_sn or
                    int(state.f, 16) < seen_fn or
                    (int(state.s, 16) == seen_sn and state.d != seen_said)):
                    raise ChangeUnavailable("The witness key state changed behind the index. "
                                            "Retry after the next sweep.")
                events = []
                for event in rdb.clonePreIter(pre=aid.encode(), fn=fn, gvrsn=_GENUS):
                    replay_bytes += len(event)
                    if replay_bytes > MAX_PAGE_REPLAY_BYTES:
                        raise ChangePageTooLarge(
                            f"The selected AIDs need more than {MAX_PAGE_REPLAY_BYTES} replay "
                            "bytes. Reduce limit; a single AID above this bound cannot be "
                            "served by this feed."
                        )
                    events.append(event)
                if len(events) != int(state.f, 16) - fn + 1:
                    raise ChangeUnavailable("The witness could not replay every indexed event. "
                                            "Retry after the database is repaired.")
                change.append({"aid": aid, "fn": fn,
                               "kel": base64.b64encode(b"".join(events)).decode()})
        finally:
            if rdb is not None:
                rdb.close()
        with self._lock:
            if generation != self.generation:
                raise CursorConflict("The change index was replaced while this page was being "
                                     "read. Start again without since.")
            next_cursor = (self._encode(bucket, rows[-1][0], answer) if more else
                           self._encode(answer))
            return {"generation": generation, "next": next_cursor, "more": more,
                    "change": change}

    def change_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM change").fetchone()[0]
