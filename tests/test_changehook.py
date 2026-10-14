"""The three keripy write hooks are the entire in-loop change-feed surface."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from witness import changehook
from witness.changelog import ChangeLog, LogReader


class Store:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def append(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    add = append
    put = append


class Log:
    def __init__(self):
        self.entries = []
        self.failures = 0

    def append(self, aid, digest, fn=None):
        self.entries.append((aid, digest) if fn is None else (aid, digest, fn))

    def fail(self):
        self.failures += 1


def test_real_kevery_event_and_old_receipt_both_reach_the_hook(tmp_path):
    """A receipt on an older event matters even after a later event was accepted."""
    from keri.app import habbing
    from keri.core import Kevery, Parser

    witness = habbing.Habery(
        name="changehook", base="", temp=False, headDirPath=str(tmp_path),
        bran="abcdefghijk1234567890",
    )
    controller = habbing.Habery(name="changehookctl", temp=True, bran="0987654321kjihgfedcba")
    log = ChangeLog.open(tmp_path / "change")
    try:
        whab = witness.makeHab(name="wit", transferable=False)
        changehook.install(witness.db, log)
        chab = controller.makeHab(name="ctl", transferable=True, wits=[whab.pre], toad=1)
        kvy = Kevery(db=witness.db, lax=False, local=False)
        Parser().parse(ims=bytearray(chab.msgOwnInception(framed=True)), kvy=kvy, local=True)
        first_said = chab.kever.serder.said
        first_serder = chab.kever.serder

        chab.rotate()
        Parser().parse(ims=bytearray(chab.msgOwnEvent(sn=1)), kvy=kvy, local=True)
        second_said = chab.kever.serder.said

        first_receipt = whab.receipt(first_serder, framed=True)
        Parser().parse(ims=bytearray(first_receipt), kvy=kvy, local=True)
        with log.env.begin() as txn:
            count = txn.stat(db=log.entries)["entries"]
        Parser().parse(ims=bytearray(first_receipt), kvy=kvy, local=True)
        with log.env.begin() as txn:
            assert txn.stat(db=log.entries)["entries"] == count
        log.close_clean()
        log = None
        with LogReader(tmp_path / "change") as reader:
            assert [(aid, said) for _, aid, said, _, _ in reader.entries_after(0)] == [
                (chab.pre, first_said), (chab.pre, second_said), (chab.pre, first_said)
            ]
    finally:
        if log is not None:
            log.close()
        controller.close()
        witness.close()


def test_first_seen_ordinal_zero_is_stored_and_returned():
    db = SimpleNamespace(fels=Store(0), wigs=Store(False))
    log = Log()
    changehook.install(db, log)

    result = db.fels.append(keys=b"Eaid", val=b"Edigest")
    assert result == 0
    assert db.fels.calls == [((), {"keys": b"Eaid", "val": b"Edigest"})]
    assert log.entries == [("Eaid", "Edigest", 0)]


def test_fels_does_not_log_if_an_accessor_reports_no_append():
    db = SimpleNamespace(fels=Store(None), wigs=Store(False))
    log = Log()
    changehook.install(db, log)

    result = db.fels.append(keys=b"Eaid", val=b"Edigest")
    assert result is None
    assert log.entries == []


@pytest.mark.parametrize("method", ["add", "put"])
@pytest.mark.parametrize("keys", [(b"Eaid", b"Edigest"), b"Eaid.Edigest"])
def test_new_receipt_is_logged_from_both_keripy_key_forms(method, keys):
    db = SimpleNamespace(fels=Store(0), wigs=Store(True))
    log = Log()
    changehook.install(db, log)

    value = {"val": b"signature"} if method == "add" else {"vals": [b"signature"]}
    assert getattr(db.wigs, method)(keys=keys, **value) is True
    assert log.entries == [("Eaid", "Edigest")]


@pytest.mark.parametrize("method", ["add", "put"])
def test_duplicate_receipt_is_not_logged(method):
    db = SimpleNamespace(fels=Store(0), wigs=Store(False))
    log = Log()
    changehook.install(db, log)

    value = {"val": b"signature"} if method == "add" else {"vals": [b"signature"]}
    assert getattr(db.wigs, method)(keys=("Eaid", "Edigest"), **value) is False
    assert log.entries == []


def test_failed_log_append_does_not_fail_keripy_and_marks_generation_failed():
    db = SimpleNamespace(fels=Store(1), wigs=Store(False))

    class BrokenLog(Log):
        def append(self, aid, digest, fn=None):
            raise OSError("disk full")

    log = BrokenLog()
    changehook.install(db, log)

    result = db.fels.append(keys="Eaid", val="Edigest")
    assert result == 1
    assert log.failures == 1


def test_failed_failure_recording_still_does_not_fail_keripy():
    db = SimpleNamespace(fels=Store(1), wigs=Store(False))

    class BrokenLog(Log):
        def append(self, aid, digest, fn=None):
            raise OSError("disk full")

        def fail(self):
            raise OSError("still full")

    changehook.install(db, BrokenLog())
    result = db.fels.append(keys="Eaid", val="Edigest")
    assert result == 1


def test_original_exception_propagates_and_does_not_write_log():
    db = SimpleNamespace(fels=Store(0), wigs=Store(False))

    def explode(*args, **kwargs):
        raise RuntimeError("keripy failed")

    db.fels.append = explode
    log = Log()
    changehook.install(db, log)

    with pytest.raises(RuntimeError, match="keripy failed"):
        db.fels.append(keys="Eaid", val="Edigest")
    assert log.entries == []
    assert log.failures == 0


def test_hook_module_has_no_other_in_loop_calls():
    """@52dwv9ky freezes the write hook in the same spirit as @e3uji3mv."""
    tree = ast.parse(Path(changehook.__file__).read_text())
    hooked = {"_parts", "_record", "fel_append", "wig_add", "wig_put"}
    allowed = {
        "original_fel", "original_add", "original_put", "append", "fail", "_parts",
        "_record", "rsplit", "decode", "isinstance",
    }
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in hooked:
            found.add(node.name)
            for child in ast.walk(node):
                assert not isinstance(child, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith))
                if isinstance(child, ast.Call):
                    called = child.func.id if isinstance(child.func, ast.Name) else child.func.attr
                    assert called in allowed, (node.name, called)
    assert found == hooked
