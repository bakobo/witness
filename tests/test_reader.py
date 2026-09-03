"""WitnessReader — read-only liveness and identity over a real witness LMDB, plus the
unavailable and no-identity failure paths."""

import os

import keri
import pytest
from keri.app import habbing

from witness import reader as reader_mod
from witness.config import ControlPlaneConfig
from witness.errors import DbUnavailable, IdentityUnavailable
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
    assert reader.health() == {"status": "ok"}


def test_info_reports_the_witness_identity(reader, witness_db):
    info = reader.info()
    assert info == {
        "aid": witness_db["pre"],
        "alias": witness_db["alias"],
        "keripy_version": keri.__version__,
        "db_path": witness_db["db_path"],
    }
    assert isinstance(keri.__version__, str) and keri.__version__


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
        WitnessReader(unopenable_config).info()


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
        WitnessReader(cfg).info()


def test_info_raises_identityunavailable_when_no_witness_hab_is_present(reader, monkeypatch):
    monkeypatch.setattr(reader_mod, "_select_witness_hab", lambda items: None)
    with pytest.raises(IdentityUnavailable):
        reader.info()


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
    with pytest.raises(IdentityUnavailable) as caught:
        WitnessReader(cfg).info()
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
