"""Backing a witness up (@7b34ohbo).

@a24p3kbw made an upgrade across a keripy migration a one-way door, so restoring a backup IS the
rollback path, and a rollback path nobody has exercised is a hope rather than a plan.

The assertion that matters most is not that files appear. It is that ALL FOUR stores appear, and
in particular the keystore: a witness's signing keys do not live in the event database, so a
backup of `db` alone restores something that holds every event and cannot sign a single receipt —
dead while looking alive, which is the worst way for a backup to fail.
"""

import json
import os

import pytest
from keri.app import habbing
from keri.db import basing

from witness import backup
from witness.config import BackupConfig
from witness.errors import BackupIncomplete, DbUnavailable


@pytest.fixture
def witness_home(tmp_path):
    """A witness home with a keystore, an event database and a config file."""
    head = str(tmp_path / "keri-home")
    hby = habbing.Habery(
        name="bk", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hab = hby.makeHab(name="w", transferable=False)
    aid = hab.pre
    hby.close()
    config_dir = tmp_path / "keri-home" / "keri" / "cf"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bk.json").write_text('{"dt": "2026-09-04T00:00:00.000000+00:00"}')
    return head, aid


def _config(head, destination, **kwargs):
    return BackupConfig(
        name="bk", base="", head_dir_path=head, destination=str(destination), **kwargs
    )


def test_the_backup_mirrors_the_keri_layout_so_restore_is_a_plain_copy(witness_home, tmp_path):
    """Restore is deliberately not a subcommand — it writes into the witness's own volume, which
    is the direction this repo exists to avoid. That only works if the backup is shaped like the
    thing it restores into, so an operator's `cp -a` needs no translation step to get wrong."""
    head, _aid = witness_home
    destination = tmp_path / "backup"

    backup.back_up(_config(head, destination))

    assert (destination / "ks" / "bk" / "data.mdb").is_file()
    assert (destination / "db" / "bk" / "data.mdb").is_file()
    assert (destination / "cf" / "bk.json").is_file()


def test_the_keystore_is_backed_up_and_not_only_the_events(witness_home, tmp_path):
    """The failure this whole design guards against. A backup of the event database alone
    restores a witness holding every event it ever receipted and unable to sign a new one."""
    head, _aid = witness_home
    destination = tmp_path / "backup"

    manifest = backup.back_up(_config(head, destination))

    assert "keystore" in manifest["stores"]
    assert manifest["stores"]["keystore"]["bytes"] > 0


def test_a_missing_keystore_fails_the_backup_rather_than_producing_a_partial_one(tmp_path):
    """A backup that quietly omits a store is worse than no backup: it is discovered during a
    restore, which is the moment the alternative is gone."""
    head = str(tmp_path / "no-keystore")
    hby = habbing.Habery(name="bk", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890")
    hby.makeHab(name="w", transferable=False)
    hby.close()
    # Remove the keystore, leaving the event database intact.
    import shutil

    shutil.rmtree(os.path.join(head, "keri", "ks", "bk"))

    with pytest.raises(BackupIncomplete) as caught:
        backup.back_up(_config(head, tmp_path / "backup"))

    assert "keystore" in str(caught.value)
    assert caught.value.retryable is False


def test_a_missing_database_fails_closed(tmp_path):
    head = str(tmp_path / "nothing-here")
    os.makedirs(head)

    with pytest.raises(DbUnavailable):
        backup.back_up(_config(head, tmp_path / "backup"))


def test_an_absent_registry_is_recorded_rather_than_invented(witness_home, tmp_path):
    """A plain witness has no credential registry until something creates one. Its absence is a
    fact about this deployment, so the manifest says so instead of the backup failing or, worse,
    silently looking complete."""
    head, _aid = witness_home
    destination = tmp_path / "backup"

    manifest = backup.back_up(_config(head, destination))

    assert manifest["stores"]["registry"]["present"] is False
    assert not (destination / "reg").exists()


def test_the_manifest_records_what_a_restore_needs_to_be_checked_against(witness_home, tmp_path):
    """Restoring into a build with an older keripy does not work — keripy refuses a database
    written by a newer library (@a24p3kbw). So the backup records the version it was taken with,
    which is what turns that refusal from a surprise into something checkable beforehand."""
    import keri

    head, aid = witness_home
    destination = tmp_path / "backup"

    manifest = backup.back_up(_config(head, destination))

    assert manifest["keripy_version"] == keri.__version__
    assert manifest["witness_aid"] == aid
    assert manifest["name"] == "bk"
    on_disk = json.loads((destination / "manifest.json").read_text())
    assert on_disk == manifest


def test_the_source_is_opened_read_only_and_left_untouched(witness_home, tmp_path):
    """@k3p7wr is not suspended because the operation is administrative. A backup that opened the
    witness's stores read-write would be able to corrupt the thing it is protecting."""
    head, _aid = witness_home
    source = os.path.join(head, "keri", "db", "bk", "data.mdb")
    before = (os.path.getmtime(source), os.path.getsize(source))

    backup.back_up(_config(head, tmp_path / "backup"))

    assert (os.path.getmtime(source), os.path.getsize(source)) == before


def test_the_copied_database_opens_and_holds_the_same_identity(witness_home, tmp_path):
    """The only assertion that proves a backup is worth anything: the copy is a database, not a
    pile of bytes of the right size."""
    head, aid = witness_home
    destination = tmp_path / "backup"
    backup.back_up(_config(head, destination))

    # A restore is a copy back into a keri home, so read the copy exactly as a witness would.
    restored_head = tmp_path / "restored"
    (restored_head / "keri").mkdir(parents=True)
    import shutil

    shutil.copytree(destination / "db", restored_head / "keri" / "db")
    shutil.copytree(destination / "ks", restored_head / "keri" / "ks")

    rdb = basing.Baser(name="bk", base="", temp=False, headDirPath=str(restored_head), reopen=False)
    rdb.reopen(readonly=True)
    try:
        habs = list(rdb.habs.getTopItemIter())
    finally:
        rdb.close()

    assert [record.hid for _keys, record in habs] == [aid]


def test_an_existing_backup_is_not_silently_overwritten(witness_home, tmp_path):
    """Overwriting the previous backup with a new one is how somebody ends up with exactly zero
    good backups at the moment they need one."""
    head, _aid = witness_home
    destination = tmp_path / "backup"
    backup.back_up(_config(head, destination))

    with pytest.raises(BackupIncomplete):
        backup.back_up(_config(head, destination))


def test_an_existing_backup_can_be_replaced_deliberately(witness_home, tmp_path):
    head, _aid = witness_home
    destination = tmp_path / "backup"
    backup.back_up(_config(head, destination))

    manifest = backup.back_up(_config(head, destination, force=True))

    assert manifest["stores"]["database"]["bytes"] > 0


def test_a_witness_that_has_not_incepted_yet_can_still_be_backed_up(tmp_path):
    """A keystore exists from `kli init`, before the witness has an AID. Backing that up is
    legitimate — it holds the keys — and the manifest says there is no AID rather than guessing."""
    head = str(tmp_path / "not-incepted")
    hby = habbing.Habery(
        name="bk", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hby.close()  # stores created, no hab made

    manifest = backup.back_up(_config(head, tmp_path / "backup"))

    assert manifest["witness_aid"] is None
    assert manifest["stores"]["keystore"]["present"] is True


def test_an_absent_config_file_is_recorded_rather_than_faked(tmp_path):
    """`kli init` writes cf/<name>.json, but a witness configured entirely by flags has none.
    Its absence belongs in the manifest, so a restore can tell "there was no config" from
    "the config was lost"."""
    head = str(tmp_path / "no-config")
    hby = habbing.Habery(
        name="bk", base="", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hby.makeHab(name="w", transferable=False)
    hby.close()

    manifest = backup.back_up(_config(head, tmp_path / "backup"))

    assert manifest["stores"]["config"] == {"present": False}


def test_a_base_subdirectory_is_carried_through_the_whole_backup(tmp_path):
    """keripy nests stores under an optional base, and the backup mirrors it. Getting the walk-up
    wrong here would put the config file in the wrong place while every store still looked fine."""
    head = str(tmp_path / "based")
    hby = habbing.Habery(
        name="bk", base="sub", temp=False, headDirPath=head, bran="abcdefghijk1234567890"
    )
    hby.makeHab(name="w", transferable=False)
    hby.close()
    destination = tmp_path / "backup"
    config = BackupConfig(
        name="bk", base="sub", head_dir_path=head, destination=str(destination)
    )

    manifest = backup.back_up(config)

    assert (destination / "db" / "sub" / "bk" / "data.mdb").is_file()
    assert (destination / "ks" / "sub" / "bk" / "data.mdb").is_file()
    assert manifest["base"] == "sub"


def test_home_of_a_store_that_is_not_there_is_none(tmp_path):
    """Callers ask for a home to find the config file beside the stores. When nothing resolved
    there is no home to name, and returning a plausible-looking path would send a backup to write
    into a directory that no witness has ever used."""
    from keri.db import basing

    from witness import paths

    config = BackupConfig(
        name="absent", base="", head_dir_path=str(tmp_path), destination=str(tmp_path / "b")
    )

    assert paths.home(basing.Baser, config) is None


def test_the_keystore_is_copied_last_so_a_tear_cannot_be_the_harmful_kind():
    """env.copy is atomic per ENVIRONMENT and says nothing across two, so three copies are three
    instants. The unrecoverable direction is a key history referencing a key the keystore lacks —
    a party that signs perfectly and can never rotate again — which arises only when the database
    snapshot is newer than the keystore's. Taking the keystore last makes that impossible; the
    reverse ordering, which this code originally had, makes it possible.

    Pinned as an ordering test because nothing else about the code would look wrong if someone
    tidied the tuple into alphabetical order.
    """
    labels = [label for label, _klas, _required in backup._STORES]

    assert labels[-1] == "keystore", (
        f"the keystore must be copied last, got {labels}; see the note on _STORES"
    )
    assert labels.index("database") < labels.index("keystore")
