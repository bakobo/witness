"""The telemetry segment: a fixed-size shared mapping the witness writes and the control plane
reads (@vxt7feoi).

Layout, little-endian, all offsets fixed at creation::

    0                MAGIC (6 bytes)
    6                layout version (uint16)
    8                slot count (uint16)
    10               name table, slot_count * 32 bytes, NUL padded
    10 + 32*n        body:
                       +0   sequence      uint64   seqlock; odd means a write is in flight
                       +8   ticks         uint64   loop passes since start
                       +16  wall          float64  wall clock at the last pass
                       +24  lag           float64  seconds the last pass ran behind its tock
                       +32  current_since float64  when the executing doer was entered
                       +40  current       int32    executing doer slot, -1 between doers
                       +48  per slot: last float64, max float64

The sequence is a seqlock rather than a mutex because the writer is the witness's own loop thread
and must never block on a reader. Readers detect a torn read and retry; the writer never waits.

Functions marked ``@hot_path`` run on that loop thread. ``@e3uji3mv`` freezes what they may do,
and ``tests/test_inloop.py::test_the_hot_path_stays_frozen`` enforces it by parsing this file.
Marking a function ``@hot_path`` opts it into that check; the set of marked functions is itself
asserted, so the decorator cannot be quietly removed.
"""

from __future__ import annotations

import mmap
import os
import struct
from struct import pack_into

from .errors import TelemetryIncompatible, TelemetryUnavailable

MAGIC = b"BKWTEL"
LAYOUT_VERSION = 1
MAX_SLOTS = 64
NAME_BYTES = 32

_HEADER = struct.Struct("<6sHH")
_NAME_TABLE_OFFSET = _HEADER.size
_BODY = struct.Struct("<QQdddi4x")
_SLOT = struct.Struct("<dd")

_SEQ_OFF = 0
_TICKS_OFF = 8
_WALL_OFF = 16
_LAG_OFF = 24
_SINCE_OFF = 32
_CURRENT_OFF = 40
_SLOTS_OFF = _BODY.size

_NO_DOER = -1


def hot_path(func):
    """Mark a function as running on the witness's Doist thread.

    A pure marker: it changes nothing at runtime and exists so the freeze test can find these
    functions. Adding it to a function opts that function into the allowlist check; the expected
    set of marked functions is asserted separately, so removing it is also a test failure.
    """
    func.__witness_hot_path__ = True
    return func


def segment_size(slots: int) -> int:
    """Total bytes a segment with ``slots`` doer entries occupies."""
    return _NAME_TABLE_OFFSET + slots * NAME_BYTES + _BODY.size + slots * _SLOT.size


def _body_offset(slots: int) -> int:
    return _NAME_TABLE_OFFSET + slots * NAME_BYTES


class SegmentWriter:
    """The witness side. Created once at startup; its hot-path methods only store into memory."""

    def __init__(self, path, mapping, slots):
        self._path = path
        self._map = mapping
        self._slots = slots
        self._body = _body_offset(slots)
        self._sequence = 0
        self._since = 0.0
        self._max = [0.0] * slots

    @classmethod
    def create(cls, path, names):
        """Create (or truncate) the segment at ``path`` with one slot per name.

        Setup, not hot path: this opens files and allocates freely. Names longer than
        :data:`NAME_BYTES` are truncated, because a doer name is a label rather than an identifier
        and refusing to start a witness over a long label would be absurd.
        """
        names = list(names)
        if len(names) > MAX_SLOTS:
            raise TelemetryIncompatible(
                f"The witness has {len(names)} doers but the telemetry segment holds at most "
                f"{MAX_SLOTS}; raise MAX_SLOTS and rebuild both sides together."
            )
        size = segment_size(len(names))
        with open(path, "wb") as handle:
            handle.write(bytes(size))
        descriptor = os.open(path, os.O_RDWR)
        try:
            mapping = mmap.mmap(descriptor, size)
        finally:
            os.close(descriptor)
        _HEADER.pack_into(mapping, 0, MAGIC, LAYOUT_VERSION, len(names))
        for index, name in enumerate(names):
            start = _NAME_TABLE_OFFSET + index * NAME_BYTES
            mapping[start:start + NAME_BYTES] = name.encode()[:NAME_BYTES].ljust(NAME_BYTES, b"\0")
        writer = cls(path, mapping, len(names))
        pack_into("<i", mapping, writer._body + _CURRENT_OFF, _NO_DOER)
        return writer

    @hot_path
    def _begin_write(self):
        self._sequence = self._sequence + 1  # odd: a write is in flight
        pack_into("<Q", self._map, self._body + _SEQ_OFF, self._sequence)

    @hot_path
    def _end_write(self):
        self._sequence = self._sequence + 1  # even: the record is coherent again
        pack_into("<Q", self._map, self._body + _SEQ_OFF, self._sequence)

    @hot_path
    def publish_tick(self, ticks, wall, lag):
        """Record one completed loop pass."""
        self._begin_write()
        pack_into("<Qdd", self._map, self._body + _TICKS_OFF, ticks, wall, lag)
        self._end_write()

    @hot_path
    def mark_enter(self, slot, now):
        """Record that ``slot``'s doer is about to run. Written BEFORE the doer is sent to, so a
        doer that never returns has already named itself."""
        self._since = now
        self._begin_write()
        pack_into("<di", self._map, self._body + _SINCE_OFF, now, slot)
        self._end_write()

    @hot_path
    def mark_exit(self, slot, now):
        """Record that ``slot``'s doer returned, and how long it took."""
        elapsed = now - self._since
        highest = max(elapsed, self._max[slot])
        self._max[slot] = highest
        self._begin_write()
        pack_into("<i", self._map, self._body + _CURRENT_OFF, _NO_DOER)
        pack_into("<dd", self._map, self._body + _SLOTS_OFF + slot * 16, elapsed, highest)
        self._end_write()

    def close(self):
        self._map.close()


class SegmentReader:
    """The control-plane side. Maps the segment PROT_READ, so it cannot write even by mistake.

    This is the guarantee @a24p3kbw could not obtain for LMDB, which needs a writable lock file.
    A purpose-built segment has no lock protocol, so the read-only mapping costs nothing.
    """

    _on_retry = None

    def __init__(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError as exc:
            raise TelemetryUnavailable(
                "The witness telemetry segment was not found; the witness may not be running yet."
            ) from exc
        try:
            self._map = mmap.mmap(descriptor, 0, prot=mmap.PROT_READ)
        finally:
            os.close(descriptor)
        magic, version, slots = _HEADER.unpack_from(self._map, 0)
        if magic != MAGIC:
            raise TelemetryIncompatible(
                "That file is not a witness telemetry segment; its magic number does not match."
            )
        if version != LAYOUT_VERSION:
            raise TelemetryIncompatible(
                f"The telemetry segment uses layout version {version} but this build reads "
                f"version {LAYOUT_VERSION}; the witness and the control plane must be upgraded "
                f"together."
            )
        self._slots = slots
        self._body = _body_offset(slots)
        self._names = [
            self._map[
                _NAME_TABLE_OFFSET + index * NAME_BYTES :
                _NAME_TABLE_OFFSET + (index + 1) * NAME_BYTES
            ].rstrip(b"\0").decode(errors="replace")
            for index in range(slots)
        ]

    def read(self, attempts=5):
        """Return a coherent snapshot, retrying past writes in flight."""
        for _ in range(attempts):
            before = struct.unpack_from("<Q", self._map, self._body + _SEQ_OFF)[0]
            if before % 2 == 0:
                snapshot = self._snapshot()
                after = struct.unpack_from("<Q", self._map, self._body + _SEQ_OFF)[0]
                if after == before:
                    return snapshot
            if self._on_retry is not None:
                self._on_retry()
        raise TelemetryUnavailable(
            "The witness telemetry segment was being written throughout every read attempt; "
            "try again."
        )

    def _snapshot(self):
        _seq, ticks, wall, lag, since, current = _BODY.unpack_from(self._map, self._body)
        doers = []
        for index in range(self._slots):
            last, highest = _SLOT.unpack_from(self._map, self._body + _SLOTS_OFF + index * 16)
            doers.append(
                {"name": self._names[index], "last_seconds": last, "max_seconds": highest}
            )
        return {
            "ticks": ticks,
            "last_tick_wall": wall,
            "loop_lag": lag,
            "current_doer": None if current == _NO_DOER else self._names[current],
            "current_since": since,
            "doers": doers,
        }

    def close(self):
        self._map.close()
