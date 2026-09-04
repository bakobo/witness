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

The residual, stated plainly because it is the interesting part. ``env.copy`` is atomic per
environment and promises nothing across two, so three copies are three instants and a backup taken
while the witness writes could in principle be internally torn. The store ORDER below makes the
only harmful direction of that tear impossible, and a witness's keystore is measurably static
during normal operation because a witness AID cannot rotate. What is NOT offered is heti's
stronger guarantee for its lockbox — quiesce the stores, hold an exclusive lock across the whole
copy — because that costs downtime, and this design's premise is a backup you can take from a
running witness. If a witness ever gains a rotating key, revisit that trade rather than this
comment.
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

#: (label, keripy class, required). The directory each lands in comes from the class itself via
#: :func:`witness.paths.kind`, never from a layout assumed here.
#:
#: THE ORDER IS LOAD-BEARING, and the keystore is last on purpose. ``env.copy`` is atomic per
#: ENVIRONMENT and says nothing about two, so copying three stores gives three instants, and a
#: tear between them is not hypothetical: heti measured a keystore out of step with its own key
#: history in 17 of 30 trials under concurrent rotation, producing a party that signs perfectly
#: and can never rotate again — the one loss KERI calls unrecoverable.
#:
#: The tear only bites in one direction. The unrecoverable state is a key HISTORY referencing a
#: key the keystore does not hold, so it arises when the database snapshot is NEWER than the
#: keystore snapshot. Take the keystore last and that cannot happen: the keystore is at least as
#: new as the database, and a keystore holding a key no event references yet is inert.
#:
#: Measured here, and the reason this is a defence in depth rather than the whole answer: a
#: witness's own keystore is byte-identical after receipting other controllers' events, because a
#: witness AID is non-transferable and therefore never rotates. So the window is normally empty.
#: "Normally empty" is a circumstance; the ordering is a property, and the ordering is free.
#:
#: ``required`` is the whole safety property. The keystore and the event database are what a
#: witness IS; a credential registry is created only by something that uses one, so its absence is
#: a fact about this deployment rather than a broken backup.
_STORES = (
    ("database", basing.Baser, True),
    ("registry", Reger, False),
    ("keystore", keeping.Keeper, True),
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
