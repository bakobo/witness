"""The telemetry segment — a fixed-size shared mapping the witness writes and the control plane
reads (@vxt7feoi).

Two properties carry the design. A reader must never believe a half-written record, which the
seqlock provides; and a reader must never be able to write, which is why it maps PROT_READ and is
tested for it. The second is the guarantee @a24p3kbw could not get for LMDB — LMDB needs a
writable lock file, a purpose-built segment needs nothing — so here it is real.
"""

import mmap
import struct

import pytest

from witness import telemetry
from witness.errors import TelemetryIncompatible, TelemetryUnavailable


@pytest.fixture
def segment(tmp_path):
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["alpha", "beta"])
    yield path, writer
    writer.close()


def test_round_trip_reports_what_was_published(segment):
    path, writer = segment
    writer.publish_tick(ticks=7, wall=1234.5, lag=0.125)
    writer.mark_enter(0, now=10.0)
    writer.mark_exit(0, now=10.25)

    state = telemetry.SegmentReader(str(path)).read()

    assert state["ticks"] == 7
    assert state["last_tick_wall"] == 1234.5
    assert state["loop_lag"] == 0.125
    assert state["current_doer"] is None
    assert state["doers"][0] == {"name": "alpha", "last_seconds": 0.25, "max_seconds": 0.25}
    assert state["doers"][1]["name"] == "beta"


def test_max_duration_is_a_high_water_mark(segment):
    path, writer = segment
    for start, end in ((0.0, 0.5), (1.0, 1.1), (2.0, 0.3 + 2.0)):
        writer.mark_enter(1, now=start)
        writer.mark_exit(1, now=end)

    doer = telemetry.SegmentReader(str(path)).read()["doers"][1]

    assert doer["last_seconds"] == pytest.approx(0.3)
    assert doer["max_seconds"] == pytest.approx(0.5)


def test_a_doer_still_executing_is_named(segment):
    """The forensic payload: when the loop wedges, the segment already names the culprit,
    because the slot is written BEFORE the send rather than after it returns."""
    path, writer = segment
    writer.mark_enter(1, now=5.0)

    state = telemetry.SegmentReader(str(path)).read()

    assert state["current_doer"] == "beta"
    assert state["current_since"] == 5.0


def test_a_torn_write_is_detected_rather_than_believed(segment, monkeypatch):
    path, writer = segment
    writer.publish_tick(ticks=1, wall=1.0, lag=0.0)
    reader = telemetry.SegmentReader(str(path))
    # Leave the sequence odd, which is exactly what a reader sees mid-write.
    writer._begin_write()

    with pytest.raises(TelemetryUnavailable):
        reader.read(attempts=3)


def test_a_read_that_races_one_write_still_succeeds(segment):
    """A torn read is retried, not fatal — otherwise every concurrent read would fail."""
    path, writer = segment
    writer.publish_tick(ticks=3, wall=2.0, lag=0.0)
    reader = telemetry.SegmentReader(str(path))
    writer._begin_write()  # the reader's first look lands mid-write
    reader._on_retry = writer._end_write

    assert reader.read(attempts=3)["ticks"] == 3


def test_the_reader_maps_the_segment_read_only(segment):
    path, _writer = segment
    reader = telemetry.SegmentReader(str(path))

    with pytest.raises((TypeError, OSError, mmap.error)):
        reader._map[0:1] = b"x"


def test_a_missing_segment_is_unavailable_and_retryable(tmp_path):
    reader_path = str(tmp_path / "absent")

    with pytest.raises(TelemetryUnavailable) as excinfo:
        telemetry.SegmentReader(reader_path)

    assert excinfo.value.retryable is True


def test_a_foreign_segment_is_refused(tmp_path):
    path = tmp_path / "foreign"
    path.write_bytes(b"NOTMINE" + bytes(4096))

    with pytest.raises(TelemetryIncompatible) as excinfo:
        telemetry.SegmentReader(str(path))

    assert excinfo.value.retryable is False


def test_a_future_layout_version_is_refused(tmp_path, segment):
    path, writer = segment
    raw = bytearray(path.read_bytes())
    struct.pack_into("<H", raw, len(telemetry.MAGIC), telemetry.LAYOUT_VERSION + 1)
    other = path.parent / "future"
    other.write_bytes(bytes(raw))

    with pytest.raises(TelemetryIncompatible):
        telemetry.SegmentReader(str(other))


def test_more_names_than_the_segment_holds_is_refused(tmp_path):
    with pytest.raises(TelemetryIncompatible):
        telemetry.SegmentWriter.create(
            str(tmp_path / "too-many"), names=[f"d{i}" for i in range(telemetry.MAX_SLOTS + 1)]
        )


def test_long_names_are_truncated_rather_than_rejected(tmp_path):
    """A doer name is a label, not an identifier; losing its tail is better than refusing to run."""
    writer = telemetry.SegmentWriter.create(
        str(tmp_path / "long"), names=["x" * (telemetry.NAME_BYTES + 40)]
    )
    try:
        name = telemetry.SegmentReader(str(tmp_path / "long")).read()["doers"][0]["name"]
    finally:
        writer.close()

    assert name == "x" * telemetry.NAME_BYTES


def test_a_write_that_lands_mid_snapshot_is_discarded(segment):
    """The sequence is re-read AFTER the snapshot: if it moved, the snapshot straddled a write."""
    path, writer = segment
    writer.publish_tick(ticks=1, wall=1.0, lag=0.0)
    reader = telemetry.SegmentReader(str(path))
    original = reader._snapshot

    def snapshot_then_let_a_write_land():
        result = original()
        writer.publish_tick(ticks=99, wall=2.0, lag=0.0)
        return result

    reader._snapshot = snapshot_then_let_a_write_land

    with pytest.raises(TelemetryUnavailable):
        reader.read(attempts=2)


def test_the_reader_releases_its_mapping(segment):
    path, _writer = segment
    reader = telemetry.SegmentReader(str(path))

    reader.close()

    assert reader._map.closed
