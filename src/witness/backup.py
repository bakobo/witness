"""Backing a witness up, consistently and without stopping it (@7b34ohbo).

@a24p3kbw established that an upgrade across a keripy migration is a one-way door: keripy refuses
to open a database written by a newer library, so a bad upgrade cannot be undone by redeploying
the previous digest. Restoring a backup IS the rollback path, which makes this the code that
rollback plan rests on.

Two things make it more than a file copy.

LMDB's own ``env.copy`` takes a **transactionally consistent** snapshot, and it works from a
read-only environment — so the witness keeps running and keeps receipting while the backup is
taken, and the result is a coherent database rather than one that merely happens to be
recoverable. Read-only because @k3p7wr's guarantee is not suspended just because the operation is
administrative.

And a witness is more than its events. The signing keys live in the KEYSTORE, a separate LMDB.
A backup of the event database alone restores a witness that holds every event it ever receipted
and cannot sign a single new one — dead while looking alive. So this copies every store there is,
and refuses rather than producing a partial backup, because a partial backup is discovered during
a restore, which is the moment the alternative is gone.
"""

from __future__ import annotations

import json
import os
import shutil
import time

import keri
from keri.app import keeping
from keri.db import basing
from keri.vdr import Reger

import witness as _witness_package

from . import paths
from .errors import BackupIncomplete, DbUnavailable

#: (label, keripy class, required). Order is copy order; the directory each lands in comes from
#: the class itself via :func:`witness.paths.kind`, never from a layout assumed here.
#:
#: ``required`` is the whole safety property. The keystore and the event database are what a
#: witness IS; a credential registry is created only by something that uses one, so its absence is
#: a fact about this deployment rather than a broken backup.
_STORES = (
    ("keystore", keeping.Keeper, True),
    ("database", basing.Baser, True),
    ("registry", Reger, False),
)


def _open_read_only(klas, config):
    store = klas(
        name=config.name,
        base=config.base,
        temp=False,
        headDirPath=config.head_dir_path,
        reopen=False,
    )
    store.reopen(readonly=True)
    return store


def _copy_store(klas, config, destination):
    """Consistent snapshot of one store into ``destination``. Returns bytes written."""
    store = _open_read_only(klas, config)
    try:
        os.makedirs(destination, exist_ok=True)
        # compact=True omits free pages, so a backup is the size of the live data rather than of
        # the high-water mark the database happens to be sitting at.
        store.env.copy(destination, compact=True)
    finally:
        store.close()
    return sum(
        os.path.getsize(os.path.join(destination, entry)) for entry in os.listdir(destination)
    )


def _witness_aid(config):
    """The witness's own AID, recorded so a restore can be checked against what was taken."""
    rdb = _open_read_only(basing.Baser, config)
    try:
        for _keys, record in rdb.habs.getTopItemIter():
            return record.hid
    finally:
        rdb.close()
    return None


def back_up(config) -> dict:
    """Copy every store into ``config.destination`` and return the manifest.

    The destination mirrors the keri home's own layout — ``ks/<name>``, ``db/<name>``,
    ``reg/<name>``, ``cf/<name>.json`` — so a restore is a directory copy an operator can read and
    audit, with no translation step to get wrong. That shape is why there is no `witness restore`
    subcommand: restoring writes into the witness's own volume, and it is a `cp -a`.
    """
    destination = config.destination
    if os.path.exists(destination) and os.listdir(destination):
        if not config.force:
            raise BackupIncomplete(
                f"{destination} already holds a backup. Overwriting the previous backup with a "
                f"new one is how an operator ends up with no good backup at the moment they need "
                f"one, so pass --force if that is genuinely what you want."
            )
        shutil.rmtree(destination)

    if paths.resolve(basing.Baser, config) is None:
        raise DbUnavailable(
            f"No witness database for {config.name!r} was found; there is nothing to back up. "
            f"Looked in: {', '.join(paths.candidates(basing.Baser, config))}."
        )

    stores = {}
    for label, klas, required in _STORES:
        source = paths.resolve(klas, config)
        if source is None:
            if required:
                raise BackupIncomplete(
                    f"The {label} for {config.name!r} is missing, so this backup would restore a "
                    f"witness that cannot function. Refusing to write a partial backup. Looked "
                    f"in: {', '.join(paths.candidates(klas, config))}."
                )
            stores[label] = {"present": False}
            continue
        target = os.path.join(destination, paths.kind(klas), config.base, config.name)
        stores[label] = {"present": True, "bytes": _copy_store(klas, config, target)}

    # The config file sits beside the stores rather than inside one, so it is a plain copy. Its
    # directory comes from wherever the database actually resolved, so it follows the same head.
    config_source = os.path.join(
        paths.home(basing.Baser, config), "cf", f"{config.name}.json"
    )
    if os.path.isfile(config_source):
        os.makedirs(os.path.join(destination, "cf"), exist_ok=True)
        shutil.copy2(config_source, os.path.join(destination, "cf", f"{config.name}.json"))
        stores["config"] = {"present": True, "bytes": os.path.getsize(config_source)}
    else:
        stores["config"] = {"present": False}

    manifest = {
        "name": config.name,
        "base": config.base,
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "witness_aid": _witness_aid(config),
        "keripy_version": keri.__version__,
        "witness_version": _witness_package.__version__,
        "stores": stores,
    }
    with open(os.path.join(destination, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest
