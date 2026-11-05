"""How big a witness process maps every keripy store it opens (@8mzxgpds).

keri-bench's witnesses filled keripy's 100 MiB at 40,000 credentials and stalled an issuer at
toad 2. These pin the size this repo chooses, the variable that overrides it, that both of
keripy's knobs carry it, and that a store which already filled has room again after a restart.
"""

import os

import lmdb
import pytest
from keri.app import habbing
from keri.db import basing, dbing

from witness import cli, stores
from witness import server as server_mod
from witness.config import ControlPlaneConfig
from witness.errors import InvalidArguments
from witness.reader import WitnessReader

KERIS = 104857600
"""keripy's own LMDBer.MapSize, which is what filled."""


def test_the_default_map_is_sixteen_gibibytes():
    assert stores.map_size({}) == stores.DEFAULT_MAP_SIZE == 16 * 1024 ** 3
    assert stores.DEFAULT_MAP_SIZE > dbing.LMDBer.MapSize


def test_the_variable_overrides_the_default():
    assert stores.map_size({stores.MAP_SIZE_ENV: " 314572800 "}) == 314572800


@pytest.mark.parametrize("value", ["", "big", "0", "-5", "1.5", "1e9", "٣", "9" * 19,
                                   str(2 ** 63), "1" * 20])
def test_a_variable_that_is_not_a_usable_byte_count_is_refused(value):
    with pytest.raises(InvalidArguments) as caught:
        stores.map_size({stores.MAP_SIZE_ENV: value})
    assert stores.MAP_SIZE_ENV in str(caught.value)


def test_the_largest_size_py_lmdb_takes_is_accepted():
    assert stores.map_size({stores.MAP_SIZE_ENV: str(2 ** 63 - 1)}) == 2 ** 63 - 1


def test_an_absurdly_long_variable_is_not_echoed_whole():
    with pytest.raises(InvalidArguments) as caught:
        stores.map_size({stores.MAP_SIZE_ENV: "7" * 5000})
    assert len(str(caught.value)) < 400


def test_applying_sets_keripys_class_and_its_own_variable(keripy_map_restored):
    stores.apply(3 * KERIS)
    assert dbing.LMDBer.MapSize == 3 * KERIS
    assert os.environ[basing.KERIBaserMapSizeKey] == str(3 * KERIS)


def test_a_stale_keripy_variable_cannot_shrink_the_database(keripy_map_restored, tmp_path):
    """Baser reads KERI_BASER_MAP_SIZE after the class, so a smaller one left in the environment
    would map the database alone at that size."""
    os.environ[basing.KERIBaserMapSizeKey] = str(KERIS)
    stores.apply(3 * KERIS)
    db = basing.Baser(name="probe", temp=False, headDirPath=str(tmp_path), reopen=True)
    try:
        assert db.env.info()["map_size"] == 3 * KERIS
    finally:
        db.close(clear=True)


def test_every_keripy_store_a_witness_opens_takes_the_size(keripy_map_restored, tmp_path):
    stores.apply(3 * KERIS)
    hby = habbing.Habery(name="probe", base="", temp=False, headDirPath=str(tmp_path),
                         bran="abcdefghijk1234567890")
    try:
        assert hby.db.env.info()["map_size"] == 3 * KERIS
        assert hby.ks.env.info()["map_size"] == 3 * KERIS
    finally:
        hby.close(clear=True)


def test_a_store_that_filled_at_keripys_size_has_room_after_a_restart(keripy_map_restored,
                                                                      tmp_path):
    small = 2 * 1024 * 1024
    stores.apply(small)
    store = dbing.LMDBer(name="probe", temp=False, headDirPath=str(tmp_path), reopen=True)
    sub = store.env.open_db(key=b"fill.")
    with pytest.raises(lmdb.MapFullError):
        for index in range(10_000):
            with store.env.begin(write=True, db=sub) as txn:
                txn.put(b"%08d" % index, b"x" * 4096)
    store.close()

    stores.apply(4 * small)
    store = dbing.LMDBer(name="probe", temp=False, headDirPath=str(tmp_path), reopen=True)
    try:
        sub = store.env.open_db(key=b"fill.")
        with store.env.begin(write=True, db=sub) as txn:
            txn.put(b"after", b"x" * 4096)
        assert store.env.info()["map_size"] == 4 * small
    finally:
        store.close(clear=True)


def test_the_control_plane_reports_the_writers_map_not_the_files_size(keripy_map_restored,
                                                                     witnessing_db):
    """A read-only open at keripy's size is raised by LMDB to the file's size, so a store past
    100 MiB would report itself full; opened at the configured size it reports that size."""
    stores.apply(3 * KERIS)
    config = ControlPlaneConfig(name="witnessing", host="127.0.0.1", port=1,
                                head_dir_path=witnessing_db["head"])
    assert WitnessReader(config).database()["map_bytes"] == 3 * KERIS


@pytest.mark.parametrize("argv", [
    ["control-plane", "--name", "testwit", "--host", "127.0.0.1", "--port", "5621"],
    ["run", "--name", "testwit", "--alias", "testwit"],
])
def test_every_process_the_cli_starts_applies_the_size_first(keripy_map_restored, monkeypatch,
                                                             argv):
    monkeypatch.setenv(stores.MAP_SIZE_ENV, str(5 * KERIS))
    seen = {}

    def note(*_args, **_kwargs):
        seen["size"] = dbing.LMDBer.MapSize
        return 0

    monkeypatch.setattr(server_mod, "serve", note)
    monkeypatch.setattr(cli.runner.WitnessRunner, "run", note)
    assert cli.main(argv) == 0
    assert seen == {"size": 5 * KERIS}


def test_a_bad_size_refuses_the_start_before_anything_opens(keripy_map_restored, monkeypatch):
    monkeypatch.setenv(stores.MAP_SIZE_ENV, "big")
    monkeypatch.setattr(cli.runner.WitnessRunner, "run",
                        lambda _self: pytest.fail("the runner started"))
    before = dbing.LMDBer.MapSize
    with pytest.raises(InvalidArguments):
        cli.main(["run", "--name", "testwit", "--alias", "testwit"])
    assert dbing.LMDBer.MapSize == before
