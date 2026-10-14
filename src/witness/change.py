"""Stateless, log-backed change pages for allow-listed watchers (@6y23f6w2)."""

from __future__ import annotations

import base64
import json
import re

import fiki
import lmdb
from keri import kering

from .changelog import LogReader
from .errors import ChangeInput, ChangePageTooLarge, ChangeUnavailable, CursorConflict

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_PAGE_ENTRIES = 10000
# ~5sqv: a complete KEL above this bound needs a future event-fragment cursor.
MAX_PAGE_REPLAY_BYTES = 16 * 1024 * 1024
_MAX_CURSOR = 1024
_GENUS = kering.Versionage(major=2, minor=0)
_HEX_CHAIN = re.compile(r"[0-9a-f]{32}\Z")


def _encode(generation, ordinal, chain, last_aid):
    raw = json.dumps([generation, ordinal, chain.hex(), last_aid], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor):
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR:
        raise ChangeInput("The since cursor is empty or longer than its bound.")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        values = json.loads(raw)
        if (not isinstance(values, list) or len(values) != 4 or
                not isinstance(values[0], str) or not values[0] or
                type(values[1]) is not int or values[1] < 0 or values[1] >= 1 << 64 or
                not isinstance(values[2], str) or _HEX_CHAIN.fullmatch(values[2]) is None or
                (values[3] is not None and (not isinstance(values[3], str) or not values[3] or
                                                 len(values[3]) > 128))):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ChangeInput("The since cursor has no usable shape.") from None
    return values[0], values[1], bytes.fromhex(values[2]), values[3]


def _replay(rdb, aid_fns):
    result = []
    used = 0
    for aid, fn in aid_fns.items():
        state = rdb.states.get(keys=aid)
        if state is None:
            raise ChangeUnavailable("The witness has no key state for a change entry. Retry after its database is repaired.")
        events = []
        for event in rdb.clonePreIter(pre=aid.encode(), fn=fn, gvrsn=_GENUS):
            used += len(event)
            if used > MAX_PAGE_REPLAY_BYTES:
                raise ChangePageTooLarge(
                    f"The selected AIDs need more than {MAX_PAGE_REPLAY_BYTES} replay bytes. Reduce limit."
                )
            events.append(event)
        if len(events) < int(state.f, 16) - fn + 1:
            raise ChangeUnavailable("The witness could not replay every indexed event. Retry after its database is repaired.")
        result.append({"aid": aid, "fn": fn, "kel": base64.b64encode(b"".join(events)).decode()})
    return result


class ChangeFeed:
    """Opens the change log and, only when needed, keripy's Baser per request."""

    def __init__(self, path_fn, reader):
        self.path_fn = path_fn
        self.reader = reader
        self._key = None

    def _open_log(self):
        try:
            return LogReader(self.path_fn())
        except (FileNotFoundError, OSError, RuntimeError, lmdb.Error) as exc:
            raise ChangeUnavailable("The change log is unavailable. Retry after its writer starts or recovers.") from exc

    def key(self):
        if self._key is None:
            with self._open_log() as log:
                self._key = fiki.Key.from_seed(log.seed)
        return self._key

    def page(self, since, limit=DEFAULT_LIMIT):
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ChangeInput(f"The limit must be an integer from 1 through {MAX_LIMIT}.")
        position = None if since is None else _decode(since)
        with self._open_log() as log:
            last_ordinal, last_chain = log.last()
            if position is not None:
                generation, ordinal, chain, last_aid = position
                if (generation != log.generation or ordinal > last_ordinal or
                        log.chain_at(ordinal) != chain):
                    raise CursorConflict("The since cursor does not match this change history. Start again without since.")
            else:
                ordinal, chain, last_aid = last_ordinal, last_chain, ""
            if last_aid is not None:
                return self._initial(log, ordinal, chain, last_aid, limit)
            if ordinal == last_ordinal:
                return {"generation": log.generation,
                        "next": _encode(log.generation, ordinal, chain, None),
                        "more": False, "change": []}
            return self._poll(log, ordinal, last_ordinal, limit)

    def _initial(self, log, ordinal, chain, last_aid, limit):
        rdb = self.reader._open()
        try:
            aids = []
            with rdb.env.begin() as txn:
                cursor = txn.cursor(db=rdb.states.sdb)
                if cursor.set_range(last_aid.encode()):
                    for key, _value in cursor:
                        aid = key.decode()
                        if aid <= last_aid:
                            continue
                        aids.append(aid)
                        if len(aids) > limit:
                            break
            more = len(aids) > limit
            selected = aids[:limit]
            changes = _replay(rdb, dict.fromkeys(selected, 0))
        finally:
            rdb.close()
        return {"generation": log.generation,
                "next": _encode(log.generation, ordinal, chain, selected[-1] if more else None),
                "more": more, "change": changes}

    def _poll(self, log, ordinal, last_ordinal, limit):
        rdb = self.reader._open()
        try:
            aid_fns = {}
            consumed = ordinal
            consumed_chain = log.chain_at(ordinal)
            count = 0
            for entry_ordinal, aid, digest, entry_chain, entry_fn in log.entries_after(ordinal):
                if count >= MAX_PAGE_ENTRIES:
                    break
                fon = rdb.fons.get(keys=(aid, digest))
                if fon is None and entry_fn is not None:
                    # keripy pins fons after fels.append, so this event is still being written.
                    # Stop before it rather than consume an entry the replay cannot yet carry.
                    break
                if fon is not None and aid not in aid_fns and len(aid_fns) >= limit:
                    break
                count += 1
                consumed, consumed_chain = entry_ordinal, entry_chain
                if fon is not None:
                    fn = int(fon.num)
                    aid_fns[aid] = min(aid_fns.get(aid, fn), fn)
            changes = _replay(rdb, aid_fns)
        finally:
            rdb.close()
        return {"generation": log.generation,
                "next": _encode(log.generation, consumed, consumed_chain, None),
                "more": consumed < last_ordinal, "change": changes}
