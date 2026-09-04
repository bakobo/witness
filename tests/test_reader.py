"""WitnessReader — read-only liveness and identity over a real witness LMDB, plus the
unavailable and no-identity failure paths."""

import os

import keri
import pytest
from keri.app import habbing
from keri.db import basing

from witness import reader as reader_mod
from witness import telemetry
from witness.config import ControlPlaneConfig
from witness.errors import (
    ControllerUnknown,
    TelemetryNotConfigured,
    DbUnavailable,
    ForeignKeystore,
    WitnessNotIncepted,
)
from witness.reader import WitnessReader, _select_witness_hab


@pytest.fixture(scope="module")
def witness_db(tmp_path_factory):
    """Build a persistent witness DB (non-transferable hab), then close it so it can be
    reopened read-only."""
    head = str(tmp_path_factory.mktemp("witkeri"))
    hby = habbing.Habery(
        name="testwit", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hab = hby.makeHab(name="wit", transferable=False)
    info = {"pre": hab.pre, "alias": hab.name, "db_path": hby.db.path, "head": head}
    hby.close()
    return info


@pytest.fixture
def reader(witness_db):
    cfg = ControlPlaneConfig(
        name="testwit", host="127.0.0.1", port=1, base="", head_dir_path=witness_db["head"]
    )
    return WitnessReader(cfg)


def test_health_reports_ok_when_the_db_opens(reader):
    assert reader.health() == {"status": "ok", "ticks": None}


def test_info_reports_the_witness_identity(reader, witness_db):
    """@zzbdxa re-cut /info: identity answers who the witness is, and nothing else. The keripy
    version moved to /v1/witness/version and the database path to /v1/witness/database, because
    varying either changes the shape of a different answer — the insight test in url-design.md."""
    assert reader.identity() == {"aid": witness_db["pre"], "alias": "wit"}



@pytest.fixture
def unopenable_config(tmp_path):
    """A config whose database directory holds a corrupt (non-LMDB) data.mdb, so any read-only
    open fails deterministically — the transient 'witness not readable' condition."""
    name = "corruptwit"
    dbdir = tmp_path / "keri" / "db" / name  # resolved path for base=""
    dbdir.mkdir(parents=True)
    (dbdir / "data.mdb").write_bytes(b"this is not a valid lmdb file" * 16)
    (dbdir / "lock.mdb").write_bytes(b"\x00" * 128)
    return ControlPlaneConfig(
        name=name, host="127.0.0.1", port=1, base="", head_dir_path=str(tmp_path)
    )


def test_health_raises_dbunavailable_when_the_db_cannot_open(unopenable_config):
    with pytest.raises(DbUnavailable):
        WitnessReader(unopenable_config).health()


def test_info_raises_dbunavailable_when_the_db_cannot_open(unopenable_config):
    with pytest.raises(DbUnavailable):
        WitnessReader(unopenable_config).identity()


def test_health_reports_unavailable_and_creates_nothing_when_db_missing(tmp_path):
    """A missing witness DB must report unavailable AND must not create a phantom env — the
    read-only isolation guarantee. (Opening a missing read-only Baser otherwise creates one.)"""
    head = tmp_path / "empty_head"  # does not exist
    cfg = ControlPlaneConfig(
        name="ghostwit", host="127.0.0.1", port=1, base="", head_dir_path=str(head)
    )
    with pytest.raises(DbUnavailable):
        WitnessReader(cfg).health()
    assert not head.exists()  # nothing was created on disk


def test_info_reports_unavailable_when_db_missing_with_default_head():
    """Covers the default-head (head_dir_path=None) resolution branch: a uniquely-named DB
    exists under neither the primary nor the ~/.keri alt head, so it is reported unavailable."""
    cfg = ControlPlaneConfig(
        name="ghostwit-" + os.urandom(6).hex(), host="127.0.0.1", port=1, base="",
        head_dir_path=None,
    )
    with pytest.raises(DbUnavailable):
        WitnessReader(cfg).identity()


def test_info_raises_identityunavailable_when_no_witness_hab_is_present(reader, monkeypatch):
    monkeypatch.setattr(reader_mod, "_select_witness_hab", lambda items: None)
    with pytest.raises(ForeignKeystore):
        reader.identity()


# Real qb64 prefixes. A witness AID is ALWAYS non-transferable (a 'B' prefix): a transferable
# witness would need witnesses of its own, which does not terminate. ~2lmg
_NONTRANSFERABLE = "BMf2Nt2hPB_703-Y6mfxMX41KA2JFaxBQOffe7pVkzxy"
_TRANSFERABLE = "EF-IpWnScgYcmeXE-ph7pObTIrW-gB9HkNaJRxCSrl8K"


class _FakeHab:
    def __init__(self, mid, hid=_NONTRANSFERABLE):
        self.mid = mid
        self.hid = hid


def test_select_witness_hab_skips_group_habs_and_returns_the_first_local_hab():
    group = _FakeHab(mid="EGroup")
    local = _FakeHab(mid=None)
    chosen = _select_witness_hab([("k1", group), ("k2", local)])
    assert chosen is local


def test_select_witness_hab_returns_none_when_there_is_no_local_hab():
    assert _select_witness_hab([]) is None
    assert _select_witness_hab([("k1", _FakeHab(mid="EGroup"))]) is None


def test_select_witness_hab_skips_a_transferable_hab():
    """A transferable AID cannot be a witness's own identity, so it is never selected even
    though it is local rather than a group hab."""
    transferable = _FakeHab(mid=None, hid=_TRANSFERABLE)
    assert _select_witness_hab([("k1", transferable)]) is None


def test_select_witness_hab_prefers_the_nontransferable_hab_over_a_transferable_one():
    transferable = _FakeHab(mid=None, hid=_TRANSFERABLE)
    witness = _FakeHab(mid=None, hid=_NONTRANSFERABLE)
    chosen = _select_witness_hab([("k1", transferable), ("k2", witness)])
    assert chosen is witness


def test_info_refuses_a_controllers_keystore_instead_of_reporting_its_aid(tmp_path):
    """The bug ~2lmg names, against a real keystore rather than a fake.

    Pointed at a controller's keystore -- a transferable AID, not a witness at all -- /info
    reported that controller's AID as the witness's identity. The control plane exists to report
    what this witness is; reporting somebody else's identity is worse than reporting nothing.
    """
    head = str(tmp_path / "controllerstore")
    hby = habbing.Habery(
        name="notawitness", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    controller = hby.makeHab(name="alice", transferable=True)
    hby.close()

    cfg = ControlPlaneConfig(
        name="notawitness", host="127.0.0.1", port=1, base="", head_dir_path=head
    )
    with pytest.raises(ForeignKeystore) as caught:
        WitnessReader(cfg).identity()
    assert controller.pre not in str(caught.value)


def test_open_yields_a_genuinely_readonly_environment(reader):
    """@k3p7wr claims the control plane "physically cannot ... corrupt" the witness data. That is
    a property of the LMDB environment, not of our restraint, so assert the property.

    keripy makes this easy to get wrong: ``Baser(reopen=True, readonly=True)`` silently discards
    the flag. ``LMDBer.__init__`` consumes ``readonly`` and sets ``self.readonly``, then hands the
    remaining kwargs to ``Filer.__init__``, which calls ``reopen()`` without it — and
    ``LMDBer.reopen``'s ``readonly=False`` default then overwrites the value that was just set,
    because its ``if readonly is not None`` guard can never be False. The environment comes back
    read-WRITE while ``Baser.readonly`` reads False, so nothing complains. Same root cause as
    ~5s3e, seen from the other side: a flag that never reaches ``lmdb.open`` cannot prevent
    creation either.
    """
    rdb = reader._open()
    try:
        assert rdb.env.flags()["readonly"] is True
        with pytest.raises(Exception) as excinfo:
            with rdb.env.begin(write=True):
                pass  # pragma: no cover - the begin() above is what raises
        assert "read-only" in str(excinfo.value).lower()
    finally:
        rdb.close()


def test_open_still_registers_in_the_lock_table(reader):
    """@v27j7uvo: a reader that is not in the lock table reads pages the writer is recycling.
    Read-only must not be bought by losing registration — num_readers stays non-zero."""
    rdb = reader._open()
    try:
        assert rdb.env.info()["num_readers"] >= 1
    finally:
        rdb.close()


# ------------------------------------------------------------------------------------------
# Phase 2 views
# ------------------------------------------------------------------------------------------


def test_version_reports_both_halves_of_what_is_running(reader):
    """The package and the keripy it wraps, because a bug report needs both and @w7c4mz pins
    keripy by commit rather than by version."""
    import keri as keri_pkg

    import witness as witness_pkg

    assert reader.version() == {
        "witness": witness_pkg.__version__,
        "keripy": keri_pkg.__version__,
    }


def test_escrow_reports_a_depth_for_every_store_including_query_not_found(reader):
    """Infra asked for query-not-found by name: keripy re-walks that whole escrow on every hio
    loop pass, so its depth is what turns a query flood from a code reading into an observation."""
    escrow = reader.escrow()

    assert "query_not_found" in escrow["depths"]
    assert escrow["total"] == sum(escrow["depths"].values())
    assert all(count >= 0 for count in escrow["depths"].values())


def test_escrow_skips_a_store_an_upstream_bump_removed(reader, monkeypatch):
    """@w7c4mz rides semi-internal accessors. If one moves, reporting the rest honestly beats
    failing the whole endpoint over a store nobody asked about."""
    monkeypatch.setitem(reader_mod._ESCROWS, "invented", "nosuchstore")

    assert "invented" not in reader.escrow()["depths"]


def test_database_reports_usage_against_keripys_fixed_map_ceiling(reader):
    """keripy pins MapSize at 100 MB, and a witness that reaches it stops accepting events. The
    fraction is the number worth alerting on, so it is computed here rather than by every caller."""
    database = reader.database()

    assert database["map_bytes"] == basing.Baser.MapSize
    assert 0 <= database["used_fraction"] <= 1
    assert database["used_bytes"] <= database["map_bytes"]
    assert database["readers"] >= 1, "the reader registers, per @v27j7uvo"


def test_controllers_includes_the_witnesss_own_key_state(reader, witness_db):
    """A witness holds key state for itself as well as for whoever it witnesses, and that is
    worth reporting rather than filtering: an operator asking what state exists wants all of it,
    and /v1/witness/identity already answers "which of these is you"."""
    listed = reader.controllers()["controllers"]

    assert witness_db["pre"] in {entry["aid"] for entry in listed}
    assert all(
        set(entry) == {"aid", "sequence_number", "said"} and entry["sequence_number"] >= 0
        for entry in listed
    )


def test_controllers_and_controller_agree_about_a_witnessed_aid(witnessing_db):
    cfg = ControlPlaneConfig(
        name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
        head_dir_path=witnessing_db["head"],
    )
    subject = WitnessReader(cfg)

    listed = subject.controllers()["controllers"]
    aids = {entry["aid"] for entry in listed}
    assert witnessing_db["controller_pre"] in aids

    one = subject.controller(witnessing_db["controller_pre"])
    assert one["aid"] == witnessing_db["controller_pre"]
    assert one["threshold"] is not None
    listed_entry = next(e for e in listed if e["aid"] == one["aid"])
    assert listed_entry["sequence_number"] == one["sequence_number"]


def test_an_unwitnessed_aid_is_a_missing_resource_not_a_failure(witnessing_db):
    cfg = ControlPlaneConfig(
        name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
        head_dir_path=witnessing_db["head"],
    )

    with pytest.raises(ControllerUnknown) as caught:
        WitnessReader(cfg).controller(witnessing_db["unwitnessed_pre"])

    assert caught.value.status == 404
    assert caught.value.retryable is False


def test_loop_without_a_configured_segment_blames_the_right_component(reader):
    """DX-F1. This used to raise DbUnavailable, so an operator running --no-telemetry was told
    "I could not open the witness database" about a database that was open and fine."""
    with pytest.raises(TelemetryNotConfigured) as caught:
        reader.loop()

    assert caught.value.status == 501
    assert "database is unaffected" in str(caught.value)


def test_loop_reports_the_segment_when_one_is_configured(reader, witness_db, tmp_path):
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["OnlyDoer"])
    writer.publish_tick(ticks=41, wall=1.0, lag=0.25)
    cfg = ControlPlaneConfig(
        name="testwit", host="127.0.0.1", port=1, base="",
        head_dir_path=witness_db["head"], telemetry_path=str(path),
    )
    try:
        loop = WitnessReader(cfg).loop()
    finally:
        writer.close()

    assert loop["ticks"] == 41
    assert loop["loop_lag"] == 0.25
    assert [d["name"] for d in loop["doers"]] == ["OnlyDoer"]


def _telemetry_config(witness_db, path):
    return ControlPlaneConfig(
        name="testwit", host="127.0.0.1", port=1, base="",
        head_dir_path=witness_db["head"], telemetry_path=str(path),
    )


def test_health_is_degraded_when_one_doer_has_held_the_loop_too_long(witness_db, tmp_path):
    """The wedge detector. A witness stuck inside one doer still opens its database perfectly,
    which is why a database-only probe stays green through the failure that has happened."""
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["Stuck"])
    writer.publish_tick(ticks=7, wall=1.0, lag=0.0)
    writer.mark_enter(0, now=1000.0)  # entered and never left
    try:
        health = WitnessReader(_telemetry_config(witness_db, path)).health(now=1060.0)
    finally:
        writer.close()

    assert health["status"] == "degraded"
    assert "Stuck" in health["reason"]
    assert "60.0s" in health["reason"]


def test_a_doer_merely_executing_is_not_a_wedge(witness_db, tmp_path):
    """Caught against a live witness: `current_doer` is set most of the time on a healthy loop,
    because sampling catches it inside *some* doer. An earlier version treated any non-null
    current_doer as a wedge and called a perfectly healthy witness degraded. Duration is what
    distinguishes the two, so duration is what is measured."""
    path = tmp_path / "telemetry"
    writer = telemetry.SegmentWriter.create(str(path), names=["Busy"])
    writer.publish_tick(ticks=306, wall=1.0, lag=0.0)
    writer.mark_enter(0, now=1000.0)
    try:
        health = WitnessReader(_telemetry_config(witness_db, path)).health(now=1000.002)
    finally:
        writer.close()

    assert health["status"] == "ok"
    assert health["ticks"] == 306


def test_health_stays_ok_when_the_witness_publishes_no_telemetry(reader, witness_db, tmp_path):
    """A witness started from stock `kli witness start` publishes none. Calling that unhealthy
    would make this endpoint a check on our own launcher rather than on the witness."""
    cfg = ControlPlaneConfig(
        name="testwit", host="127.0.0.1", port=1, base="",
        head_dir_path=witness_db["head"], telemetry_path=str(tmp_path / "absent"),
    )

    assert WitnessReader(cfg).health()["status"] == "ok"


def test_process_delegates_to_the_proc_reader(reader, monkeypatch):
    monkeypatch.setattr(reader_mod.vitals, "runner_vitals", lambda: {"pid": 4242})

    assert reader.process() == {"pid": 4242}


def test_a_keystore_with_no_identities_at_all_is_pending_not_foreign(tmp_path):
    """~2lmg's other half. A keystore that exists but holds nothing means the witness has not
    incepted yet, which resolves itself; only a keystore holding somebody ELSE's identities is
    the permanent misconfiguration. Conflating them told an operator to fix a wait."""
    head = str(tmp_path / "empty")
    hby = habbing.Habery(name="emptywit", base="", temp=False, headDirPath=head)
    hby.close()  # a keystore, but no habs were made
    cfg = ControlPlaneConfig(name="emptywit", host="127.0.0.1", port=1, base="", head_dir_path=head)

    with pytest.raises(WitnessNotIncepted) as caught:
        WitnessReader(cfg).identity()

    assert caught.value.retryable is True
    assert caught.value.status == 409
