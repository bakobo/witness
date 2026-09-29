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
    DatabaseTooNew,
    MigrationRequired,
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


def test_one_controller_is_looked_up_rather_than_found_by_scanning(witnessing_db, monkeypatch):
    """PERF-F1 (2026-09-26 panel). The key-state store is keyed by AID, so one controller is one
    lookup; walking every record to find it made the endpoint O(N) in controllers witnessed."""
    cfg = ControlPlaneConfig(
        name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
        head_dir_path=witnessing_db["head"],
    )

    def no_scan(rdb):
        raise AssertionError("controller(aid) walked every key state")

    monkeypatch.setattr(reader_mod, "_key_states", no_scan)

    assert WitnessReader(cfg).controller(witnessing_db["controller_pre"])["aid"] == (
        witnessing_db["controller_pre"])


@pytest.mark.parametrize("aid", ["B" * 600, "not an aid", "B" * 43 + "/"])
def test_a_path_that_cannot_be_an_aid_is_a_missing_resource_before_lmdb_sees_it(
        witnessing_db, aid):
    """An AID from the URL is now a database key, and LMDB refuses keys over 511 bytes with an
    exception of its own. Anything that is not qb64 is unknown here, not a server error."""
    cfg = ControlPlaneConfig(
        name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
        head_dir_path=witnessing_db["head"],
    )

    with pytest.raises(ControllerUnknown):
        WitnessReader(cfg).controller(aid)


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


# ------------------------------------------------------------------------------------------
# Upgrading across keripy pins (~7hrf)
# ------------------------------------------------------------------------------------------


def _db_at_version(tmp_path, name, version):
    """A witness database whose recorded keripy version has been moved, as a pin bump moves it."""
    head = str(tmp_path / name)
    hby = habbing.Habery(
        name=name, base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hby.makeHab(name="w", transferable=False)
    hby.close()
    writable = basing.Baser(name=name, base="", temp=False, headDirPath=head, reopen=False)
    writable.reopen(readonly=False)
    writable.version = version
    writable.close()
    return ControlPlaneConfig(name=name, host="127.0.0.1", port=1, base="", head_dir_path=head)


def test_a_database_awaiting_migration_says_so_and_says_it_is_permanent(tmp_path):
    """~7hrf. Bumping the keripy pin forward leaves the volume behind the library, and keripy
    then refuses to open it at all. Reported as "the witness may not be running yet" — which was
    both wrong and RETRYABLE — an operator or an alert would back off and wait forever for a
    condition that only `kli migrate run` clears."""
    config = _db_at_version(tmp_path, "behind", "1.1.0")

    with pytest.raises(MigrationRequired) as caught:
        WitnessReader(config).health()

    assert caught.value.retryable is False
    assert "migrate" in str(caught.value).lower()


def test_a_database_written_by_a_newer_keripy_refuses_to_open(tmp_path):
    """The rollback direction, and the one nobody writes down: once a migration has run, deploying
    the previous image on the same volume does not work. keripy refuses rather than corrupting,
    which is the right behaviour and a hard constraint on how infra rolls back."""
    config = _db_at_version(tmp_path, "ahead", "9.9.9")

    with pytest.raises(DatabaseTooNew) as caught:
        WitnessReader(config).health()

    assert caught.value.retryable is False
    assert "restore" in str(caught.value).lower() or "roll" in str(caught.value).lower()


def test_a_failed_open_does_not_leak_the_environment_into_the_next_request(tmp_path):
    """The failure that made the above hard to diagnose. keripy raises AFTER lmdb.open succeeds,
    so a Baser we never closed stayed registered in py-lmdb's per-process table; every later
    request then failed with "already open in this process" instead of the real reason. The
    control plane opens per request, so the true cause was visible only in the very first one."""
    config = _db_at_version(tmp_path, "leaky", "1.1.0")
    reader = WitnessReader(config)

    causes = []
    for _attempt in range(3):
        with pytest.raises(MigrationRequired) as caught:
            reader.health()
        causes.append(type(caught.value.__cause__).__name__)

    assert causes == ["DatabaseError"] * 3, (
        f"the reason changed between requests: {causes} — an environment leaked"
    )


class TestTags:
    """The tenth noun, and the tag member on controller/{aid} (@nlunqygr)."""

    def _reader_for(self, facts, tag_tuple, attribs=None):
        cfg = ControlPlaneConfig(
            name=facts["name"], host="127.0.0.1", port=1, base="",
            head_dir_path=facts["head"], tags=tag_tuple, attribs=attribs or {},
        )
        return WitnessReader(cfg)

    def test_tags_reports_what_the_operator_configured(self, witnessing_db):
        answer = self._reader_for(witnessing_db, ("testnet",)).tags()
        assert answer["tags"] == ["testnet"]

    def test_tags_names_where_the_value_came_from(self, witnessing_db):
        """The `source` member is the seam for the signed reply route, so it ships from day one.

        Without it, moving the origin of tags later would be a breaking change to a frozen shape
        rather than a new value in an existing member.
        """
        assert self._reader_for(witnessing_db, ()).tags()["source"] == "operator-config"

    def test_an_untagged_witness_reports_an_empty_list_not_an_absent_member(self, witnessing_db):
        """Absence is never assurance (@pmtzkn6j); an empty list says 'nothing claimed'."""
        assert self._reader_for(witnessing_db, ()).tags()["tags"] == []

    def test_tags_needs_no_database(self, tmp_path):
        """Configured tags are answerable even when the witness has not come up yet.

        Failing this endpoint on a missing database would make the one question an operator asks
        during a bad deploy — "is this the laboratory box?" — unanswerable exactly then.
        """
        cfg = ControlPlaneConfig(
            name="nosuch", host="127.0.0.1", port=1, base="",
            head_dir_path=str(tmp_path / "absent"), tags=("testnet",),
        )
        assert WitnessReader(cfg).tags()["tags"] == ["testnet"]

    def test_a_controller_inherits_this_witnesss_tag(self, witnessing_db):
        answer = self._reader_for(witnessing_db, ("testnet",)).controller(
            witnessing_db["controller_pre"]
        )
        assert answer["tags"]["derived"] == ["testnet"]
        assert answer["tags"]["from"] == [witnessing_db["witness_pre"]]
        assert answer["tags"]["unresolved"] == []

    def test_an_untagged_witness_taints_no_controller(self, witnessing_db):
        answer = self._reader_for(witnessing_db, ()).controller(witnessing_db["controller_pre"])
        assert answer["tags"]["derived"] == []
        assert answer["tags"]["from"] == [witnessing_db["witness_pre"]]

    def test_a_co_witness_we_cannot_speak_for_is_reported_unresolved(self, fully_witnessed_db):
        """The control plane does not fetch peers (@nlunqygr), so it says so rather than guessing.

        The fixture's controller designates two witnesses and this reader is only one of them.
        """
        answer = self._reader_for(fully_witnessed_db, ("testnet",)).controller(
            fully_witnessed_db["controller_pre"]
        )
        ours, theirs = fully_witnessed_db["witness_pres"]
        assert answer["tags"]["from"] == [ours]
        assert answer["tags"]["unresolved"] == [theirs]
        assert answer["tags"]["derived"] == ["testnet"]

    def test_a_keystore_we_cannot_identify_leaves_every_witness_unresolved(
        self, witnessing_db, monkeypatch
    ):
        """An unidentifiable keystore must not fail the controller endpoint.

        `tags` is an added member on a shape that already worked; a reader that cannot work out
        its own AID should say it can speak for nobody, not turn an existing 200 into a 409.
        """
        subject = self._reader_for(witnessing_db, ("testnet",))
        monkeypatch.setattr(reader_mod, "_select_witness_hab", lambda items: None)
        answer = subject.controller(witnessing_db["controller_pre"])
        assert answer["tags"]["derived"] == []
        assert answer["tags"]["from"] == []
        assert answer["tags"]["unresolved"] == [witnessing_db["witness_pre"]]


class TestAttribs:
    """The eleventh noun, and the AID inheritance it deliberately does not take part in."""

    def _reader_for(self, facts, attribs):
        cfg = ControlPlaneConfig(
            name=facts["name"], host="127.0.0.1", port=1, base="",
            head_dir_path=facts["head"], attribs=attribs,
        )
        return WitnessReader(cfg)

    def test_attribs_report_what_the_operator_configured(self, witnessing_db):
        answer = self._reader_for(witnessing_db, {"operator": "Bakobo"}).attribs()
        assert answer["attribs"] == {"operator": "Bakobo"}

    def test_attribs_name_their_source(self, witnessing_db):
        assert self._reader_for(witnessing_db, {}).attribs()["source"] == "operator-config"

    def test_an_undeclared_witness_reports_an_empty_map(self, witnessing_db):
        assert self._reader_for(witnessing_db, {}).attribs()["attribs"] == {}

    def test_attribs_need_no_database(self, tmp_path):
        cfg = ControlPlaneConfig(
            name="nosuch", host="127.0.0.1", port=1, base="",
            head_dir_path=str(tmp_path / "absent"), attribs={"operator": "Bakobo"},
        )
        assert WitnessReader(cfg).attribs()["attribs"] == {"operator": "Bakobo"}

    def test_a_controller_inherits_no_attribs(self, witnessing_db):
        """@e4ceoopg's why-not, asserted where a consumer would see it.

        Union is defined for tag names and undefined for key/value pairs, so controller/{aid}
        carries derived tags and nothing else. A future hand adding an attribs member here
        would have to delete this test to do it, which is the point.
        """
        cfg = ControlPlaneConfig(
            name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
            head_dir_path=witnessing_db["head"], attribs={"operator": "Bakobo"},
        )
        answer = WitnessReader(cfg).controller(witnessing_db["controller_pre"])
        assert "attribs" not in answer
        assert "attribs" not in answer["tags"]


class _FakeStore:
    """Stands in for keripy's Komer over decl records, keyed (aid, kind)."""

    def __init__(self, items):
        self._items = items

    def get(self, keys):
        return self._items.get(tuple(keys))


class _FakeDecl:
    def __init__(self, tags=(), attribs=None):
        self.tags = list(tags)
        self.attribs = dict(attribs or {})


class _FakeRdb:
    """A witness database whose decl store may or may not exist, as the pin dictates."""

    def __init__(self, hab_pre, decls=None, has_store=True):
        self._hab_pre = hab_pre
        if has_store:
            self.decls = _FakeStore(decls or {})

        class _Habs:
            def getTopItemIter(self):
                if hab_pre is None:
                    return iter(())
                return iter([(("k",), _FakeHab(mid=None, hid=hab_pre))])

        self.habs = _Habs()
        self.closed = False

    def close(self):
        self.closed = True


class TestSignedDeclarations:
    """Preferring a signed declaration over operator config, when one is available (@vqqh6zdk).

    The installed keripy decides whether that is possible at all: the decl stores exist only in a
    keripy carrying the reply routes, so this reads through `getattr` exactly as escrow() already
    does for a store an upstream bump may have moved. Falling back is not a degraded mode, it is
    the honest answer for a witness whose declarations are still operator configuration.
    """

    def _reader(self, tmp_path, rdb, tags=(), attribs=None):
        cfg = ControlPlaneConfig(
            name="w", host="127.0.0.1", port=1, base="", head_dir_path=str(tmp_path),
            tags=tags, attribs=attribs or {},
        )
        subject = WitnessReader(cfg)
        subject._open = lambda: rdb
        return subject

    def test_a_signed_tag_declaration_wins_over_config(self, tmp_path):
        rdb = _FakeRdb(_NONTRANSFERABLE,
                       decls={(_NONTRANSFERABLE, "tags"): _FakeDecl(tags=["testnet"])})
        answer = self._reader(tmp_path, rdb, tags=("bakobo.pool",)).tags()
        assert answer == {"tags": ["testnet"], "source": "signed-reply"}

    def test_a_signed_attrib_declaration_wins_over_config(self, tmp_path):
        rdb = _FakeRdb(_NONTRANSFERABLE,
                       decls={(_NONTRANSFERABLE, "attribs"): _FakeDecl(attribs={"operator": "B"})})
        answer = self._reader(tmp_path, rdb, attribs={"operator": "other"}).attribs()
        assert answer == {"attribs": {"operator": "B"}, "source": "signed-reply"}

    def test_the_database_is_closed_even_when_it_answers(self, tmp_path):
        rdb = _FakeRdb(_NONTRANSFERABLE,
                       decls={(_NONTRANSFERABLE, "tags"): _FakeDecl(tags=["testnet"])})
        self._reader(tmp_path, rdb).tags()
        assert rdb.closed, "the control plane opens per request and closes what it opens (@c7v3kp)"

    def test_a_keripy_without_the_decl_store_falls_back(self, tmp_path):
        """True of the pinned keripy today, so this is the live path rather than a hypothetical."""
        rdb = _FakeRdb(_NONTRANSFERABLE, has_store=False)
        answer = self._reader(tmp_path, rdb, tags=("testnet",)).tags()
        assert answer == {"tags": ["testnet"], "source": "operator-config"}

    def test_no_declaration_published_yet_falls_back(self, tmp_path):
        rdb = _FakeRdb(_NONTRANSFERABLE, decls={})
        answer = self._reader(tmp_path, rdb, tags=("testnet",)).tags()
        assert answer["source"] == "operator-config"

    def test_a_keystore_we_cannot_identify_falls_back(self, tmp_path):
        rdb = _FakeRdb(None, decls={})
        assert self._reader(tmp_path, rdb, tags=("testnet",)).tags()["source"] == "operator-config"

    def test_an_unopenable_database_still_answers_from_config(self, tmp_path):
        """@nlunqygr: an operator asks "is this the laboratory box?" precisely when things are
        broken, so a database that will not open must not take the answer away."""
        cfg = ControlPlaneConfig(
            name="nosuch", host="127.0.0.1", port=1, base="",
            head_dir_path=str(tmp_path / "absent"), tags=("testnet",),
        )
        answer = WitnessReader(cfg).tags()
        assert answer == {"tags": ["testnet"], "source": "operator-config"}


@pytest.mark.fork_only_keripy
def test_a_real_signed_declaration_is_what_the_endpoint_serves(tmp_path):
    """End to end against a genuine witness database, now that the pin carries the decl routes.

    Marked fork_only_keripy (@enyp5khx): hab.makeDeclTags exists only on the bakobo fork until
    WebOfTrust/keripy takes it, so the drift canary deselects this rather than reading its
    absence as upstream drift.

    Everything above this stubs the store, which proves the branching and proves nothing about
    keripy. This builds a real witness, has it declare itself test infrastructure through keripy's
    own reply machinery, and then reads it back the way the control plane does -- so the claim
    that /v1/witness/tags reports what a witness actually published rests on a witness actually
    publishing it.
    """
    head = str(tmp_path / "declaring")
    hby = habbing.Habery(
        name="declaring", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hab = hby.makeHab(name="wit", transferable=False)
    hab.psr.parse(bytearray(hab.makeDeclTags(tags=["testnet"])))
    hab.psr.parse(bytearray(hab.makeDeclAttribs(attribs={"operator": "Bakobo"})))
    hby.close()

    # Operator configuration deliberately disagrees, so the assertion shows which one wins.
    cfg = ControlPlaneConfig(
        name="declaring", host="127.0.0.1", port=1, base="", head_dir_path=head,
        tags=("bakobo.pool",), attribs={"operator": "someone else"},
    )
    subject = WitnessReader(cfg)

    assert subject.tags() == {"tags": ["testnet"], "source": "signed-reply"}
    assert subject.attribs() == {
        "attribs": {"operator": "Bakobo"}, "source": "signed-reply",
    }


class TestSeedFilePrecedence:
    """Signed, then flag, then seed file, per endpoint (@hjz7b7qo)."""

    def _reader(self, tmp_path, seed=None, tags=(), attribs=None):
        path = tmp_path / "decls.json"
        if seed is not None:
            path.write_text(seed)
        cfg = ControlPlaneConfig(
            name="nosuch", host="127.0.0.1", port=1, base="",
            head_dir_path=str(tmp_path / "absent"), decl_file=str(path),
            tags=tags, attribs=attribs or {},
        )
        return WitnessReader(cfg)

    def test_a_seed_file_declares_when_no_flag_does(self, tmp_path):
        subject = self._reader(tmp_path, seed='{"tags": ["testnet"]}')
        assert subject.tags() == {"tags": ["testnet"], "source": "seed-file"}

    def test_a_flag_beats_the_seed_file(self, tmp_path):
        """Whoever typed the flag is acting now; the file was written when the volume was made."""
        subject = self._reader(tmp_path, seed='{"tags": ["testnet"]}', tags=("bakobo.pool",))
        assert subject.tags() == {"tags": ["bakobo.pool"], "source": "operator-config"}

    def test_precedence_is_decided_per_endpoint(self, tmp_path):
        """A flag for one kind does not suppress the file's answer for the other."""
        subject = self._reader(
            tmp_path, seed='{"tags": ["testnet"], "attribs": {"pool": "lab"}}',
            tags=("bakobo.pool",),
        )
        assert subject.tags()["source"] == "operator-config"
        assert subject.attribs() == {"attribs": {"pool": "lab"}, "source": "seed-file"}

    def test_seed_attribs_are_served(self, tmp_path):
        subject = self._reader(tmp_path, seed='{"attribs": {"operator": "Bakobo"}}')
        assert subject.attribs() == {"attribs": {"operator": "Bakobo"}, "source": "seed-file"}

    def test_a_missing_file_is_no_declarations_not_a_failure(self, tmp_path):
        assert self._reader(tmp_path).tags() == {"tags": [], "source": "operator-config"}

    def test_a_malformed_file_does_not_take_the_witness_down(self, tmp_path):
        """Fail closed on the value, open on the service: a bad file declares nothing.

        Refusing to answer would make a typo in a provisioning script look like a dead control
        plane, which is a worse failure than declaring nothing.
        """
        assert self._reader(tmp_path, seed="{not json").tags() == {
            "tags": [], "source": "operator-config",
        }

    def test_declaring_nothing_can_be_switched_off_entirely(self, tmp_path):
        cfg = ControlPlaneConfig(
            name="nosuch", host="127.0.0.1", port=1, base="",
            head_dir_path=str(tmp_path / "absent"), decl_file=None,
        )
        assert WitnessReader(cfg).tags() == {"tags": [], "source": "operator-config"}


def test_a_seed_file_that_is_not_utf8_declares_nothing_rather_than_failing(tmp_path):
    """A UnicodeDecodeError is a ValueError, not an OSError, so it escaped the read guard.

    An operator who copied a seed file through a tool that mangled the encoding would have got a
    500 from the endpoint documented as always answering.
    """
    path = tmp_path / "decls.json"
    path.write_bytes(b'{"tags": ["\xff\xfe invalid utf-8"]}')
    cfg = ControlPlaneConfig(
        name="nosuch", host="127.0.0.1", port=1, base="",
        head_dir_path=str(tmp_path / "absent"), decl_file=str(path),
    )
    assert WitnessReader(cfg).tags() == {"tags": [], "source": "operator-config"}


def test_a_controller_inherits_the_tags_the_endpoint_reports(tmp_path):
    """The two readings of one question have to agree (@hjz7b7qo).

    Before this, controller/{aid} derived inheritance from --tag alone, so a pooled witness
    reported testnet on /v1/witness/tags while the AIDs it witnesses inherited nothing — which
    defeats the feature for the case it was built for.
    """
    head = str(tmp_path / "seeded")
    hby = habbing.Habery(
        name="seeded", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    wit = hby.makeHab(name="wit", transferable=False)
    ctl_hby = habbing.Habery(name="ctlseed", base="", temp=True, bran="0987654321kjihgfedcba")
    ctl = ctl_hby.makeHab(name="ctl", transferable=True, wits=[wit.pre], toad=1)
    wit.psr.parse(bytearray(ctl.msgOwnInception(framed=True)))
    ctl_hby.close()
    hby.close()

    seed = tmp_path / "decls.json"
    seed.write_text('{"tags": ["testnet"]}')
    cfg = ControlPlaneConfig(
        name="seeded", host="127.0.0.1", port=1, base="", head_dir_path=head,
        decl_file=str(seed),
    )
    subject = WitnessReader(cfg)

    assert subject.tags()["source"] == "seed-file"
    assert subject.controller(ctl.pre)["tags"]["derived"] == ["testnet"]


class _FakeState:
    """A key state record as _chain reads one: witnesses in `b`, delegator in `di`."""

    def __init__(self, i, b=(), di=""):
        self.i = i
        self.b = list(b)
        self.di = di


class _FakeStates:
    def __init__(self, states):
        self._states = {state.i: state for state in states}

    def get(self, keys):
        return self._states.get(keys)


class _ChainDb:
    def __init__(self, *states):
        self.states = _FakeStates(states)


class TestDelegationChain:
    """Walking a delegation chain for witnesses (@4qrayq3j).

    Driven against a fake key-state store rather than a real witness database, and the reason is
    worth stating: a genuinely delegated AID is not acceptable to a bare witness until its
    delegator anchors the delegation and the approval is exchanged, which needs keripy's full
    delegation runtime rather than a fixture. ~25n4 records that gap. What is exercised here is
    the walk itself, which is where the decisions live.
    """

    def test_an_undelegated_state_walks_one_level(self):
        state = _FakeState("EAid", b=["B1"])
        assert reader_mod._chain(_ChainDb(state), state) == (["B1"], [])

    def test_a_delegate_collects_its_delegators_witnesses_too(self):
        upper = _FakeState("EUpper", b=["B2"])
        lower = _FakeState("ELower", b=["B1"], di="EUpper")
        witnesses, unfollowed = reader_mod._chain(_ChainDb(upper, lower), lower)
        assert sorted(witnesses) == ["B1", "B2"]
        assert unfollowed == []

    def test_a_delegate_with_no_witnesses_of_its_own_is_still_tainted(self):
        """The case the rule exists for: the chain is the only thing that can reach it."""
        upper = _FakeState("EUpper", b=["B2"])
        lower = _FakeState("ELower", b=[], di="EUpper")
        witnesses, _ = reader_mod._chain(_ChainDb(upper, lower), lower)
        assert witnesses == ["B2"]

    def test_the_whole_chain_is_walked_not_only_one_level(self):
        top = _FakeState("ETop", b=["B3"])
        mid = _FakeState("EMid", b=["B2"], di="ETop")
        low = _FakeState("ELow", b=["B1"], di="EMid")
        witnesses, unfollowed = reader_mod._chain(_ChainDb(top, mid, low), low)
        assert sorted(witnesses) == ["B1", "B2", "B3"]
        assert unfollowed == []

    def test_a_delegator_we_hold_no_state_for_is_reported_unfollowed(self):
        low = _FakeState("ELow", b=["B1"], di="EAbsent")
        witnesses, unfollowed = reader_mod._chain(_ChainDb(low), low)
        assert witnesses == ["B1"]
        assert unfollowed == ["EAbsent"], "saying we could not look beats implying we did"

    def test_a_cycle_terminates_instead_of_spinning(self):
        """Not reachable through valid KERI, which cannot close a delegation loop. Reachable
        through a corrupt store, and a read-only observer must not hang on one."""
        a = _FakeState("EA", b=["B1"], di="EB")
        b = _FakeState("EB", b=["B2"], di="EA")
        witnesses, unfollowed = reader_mod._chain(_ChainDb(a, b), a)
        assert sorted(witnesses) == ["B1", "B2"]
        assert unfollowed == []

    def test_a_chain_longer_than_the_depth_guard_reports_the_remainder(self):
        depth = reader_mod._MAX_DELEGATION_DEPTH
        states = [
            _FakeState(f"E{n}", b=[f"B{n}"], di=f"E{n + 1}") for n in range(depth + 2)
        ]
        witnesses, unfollowed = reader_mod._chain(_ChainDb(*states), states[0])
        assert len(witnesses) == depth
        assert unfollowed == [f"E{depth}"], "the chain did not end, so say where we stopped"


def test_an_undelegated_controller_reports_nothing_unfollowed(witnessing_db):
    """Against a real witness database, so the member's presence is not only a fake's property."""
    cfg = ControlPlaneConfig(
        name=witnessing_db["name"], host="127.0.0.1", port=1, base="",
        head_dir_path=witnessing_db["head"], tags=("testnet",),
    )
    answer = WitnessReader(cfg).controller(witnessing_db["controller_pre"])
    assert answer["tags"]["unfollowed"] == []
    assert answer["tags"]["derived"] == ["testnet"]
