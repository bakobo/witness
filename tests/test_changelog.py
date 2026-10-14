"""Durability and cursor properties of the independent change log."""

import shutil
from types import SimpleNamespace

import lmdb
import pytest

from witness.changelog import MAP_SIZE, ChangeLog, LogReader, change_path


def test_runner_config_without_head_override_can_resolve_path():
    assert change_path(SimpleNamespace(name="not-running", base="")) is None


def test_shared_path_follows_the_resolved_keripy_home(tmp_path):
    from keri.db import basing

    cfg = SimpleNamespace(name="path-test", base="", head_dir_path=str(tmp_path))
    db = basing.Baser(name=cfg.name, base=cfg.base, temp=False,
                      headDirPath=cfg.head_dir_path, reopen=True)
    db.close()
    assert change_path(cfg).parts[-2:] == ("change", "path-test")


def test_clean_and_unclean_lifecycle(tmp_path):
    path = tmp_path / "change"
    first = ChangeLog.open(path)
    generation, seed = first.generation, first.seed
    first.append(b"A", b"A-0")
    first.close_clean()
    second = ChangeLog.open(path)
    assert (second.generation, second.seed) == (generation, seed)
    second.append("B", "B-0")
    second.close()
    third = ChangeLog.open(path)
    assert third.generation != generation
    assert third.seed == seed
    third.close_clean()
    with LogReader(path) as reader:
        assert reader.last()[0] == 2
        assert [(aid, said) for _, aid, said, _ in reader.entries_after(0)] == [
            ("A", "A-0"), ("B", "B-0")
        ]


def test_new_log_has_new_seed_and_private_directory(tmp_path):
    first = ChangeLog.open(tmp_path / "a")
    second = ChangeLog.open(tmp_path / "b")
    assert first.generation != second.generation
    assert first.seed != second.seed
    assert (tmp_path / "a").stat().st_mode & 0o777 == 0o700
    first.close_clean()
    second.close_clean()


def test_a_failed_write_turns_the_generation_over_at_once(tmp_path):
    path = tmp_path / "change"
    log = ChangeLog.open(path)
    old = log.generation
    log.append("A", "A-0")
    log.fail()
    assert log.generation != old
    log.append("B", "B-0")
    log.close_clean()
    with LogReader(path) as reader:
        assert reader.generation == log.generation
        assert reader.last()[0] == 2
    reopened = ChangeLog.open(path)
    assert reopened.generation == log.generation
    reopened.close_clean()


def test_a_failed_turnover_stops_writes_until_an_unclean_reopen(tmp_path):
    path = tmp_path / "change"
    log = ChangeLog.open(path)
    old = log.generation
    real_begin = log.env.begin
    calls = []

    def begin(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise lmdb.MapFullError("full")
        return real_begin(*args, **kwargs)

    log.env = SimpleNamespace(begin=begin, sync=log.env.sync, close=log.env.close)
    log.fail()
    assert log.failed and log.generation == old
    with pytest.raises(RuntimeError):
        log.append("A", "A-0")
    log.close_clean()
    with pytest.raises(RuntimeError):
        LogReader(path)
    reopened = ChangeLog.open(path)
    assert reopened.generation != old
    reopened.close_clean()


def test_the_writer_maps_far_more_than_lmdbs_default(tmp_path):
    log = ChangeLog.open(tmp_path / "change")
    try:
        assert log.env.info()["map_size"] == MAP_SIZE >= 1024 ** 3
    finally:
        log.close_clean()


def test_chain_detects_restored_history(tmp_path):
    path = tmp_path / "change"
    log = ChangeLog.open(path)
    log.append("A", "A-0")
    log.close_clean()
    shutil.copytree(path, tmp_path / "snapshot")
    log = ChangeLog.open(path)
    log.append("B", "B-0")
    log.close_clean()
    with LogReader(path) as reader:
        old_chain = reader.chain_at(2)
    shutil.rmtree(path)
    shutil.copytree(tmp_path / "snapshot", path)
    log = ChangeLog.open(path)
    log.append("C", "C-0")
    log.close_clean()
    with LogReader(path) as reader:
        assert reader.chain_at(2) != old_chain
        assert reader.chain_at(3) is None
        assert list(reader.entries_after(2)) == []


def test_reader_does_not_create_missing_log(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        LogReader(path)
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        LogReader(None)
