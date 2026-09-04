"""Where keripy keeps a witness's stores, resolved without opening or creating anything.

A witness is four things on disk and they do not share a directory rule. Each keripy store class
carries its own head and tail — ``keri/db``, ``keri/ks``, ``keri/reg`` under ``/usr/local/var``,
or ``.keri/db`` and friends under ``$HOME`` when the primary head is not writable — so there is no
single "keri home" to compute, and assuming one is how a backup silently finds nothing.

So nothing here hardcodes a layout. Every path comes from the store class's own constants, which
means an upstream change to where keripy puts things moves this too.

Resolution is deliberately read-only and side-effect free: it answers "is there a store here"
without opening one, because a keripy read-only open of a MISSING store creates an empty one
(~5s3e), and a probe that pollutes the filesystem is not a probe.
"""

from __future__ import annotations

import os

#: Every store has a data file by this name; its presence is what makes a directory a store
#: rather than an empty directory keripy happens to have created.
_DB_FILE = "data.mdb"


def candidates(klas, config):
    """Every directory ``klas`` could resolve to for ``config``, most preferred first.

    With an explicit head only that head is tried. With the default, keripy tries its primary head
    and falls back to the alt (home) head, so both are candidates — and they differ in more than
    the head, since the alt tail is dotted.
    """
    if config.head_dir_path is not None:
        heads = [(config.head_dir_path, klas.TailDirPath)]
    else:
        heads = [
            (klas.HeadDirPath, klas.TailDirPath),
            (klas.AltHeadDirPath, klas.AltTailDirPath),
        ]
    return [
        os.path.abspath(os.path.expanduser(os.path.join(head, tail, config.base, config.name)))
        for head, tail in heads
    ]


def resolve(klas, config):
    """The directory that actually holds this store, or ``None``.

    Guarding on the real data file is what keeps a caller honest: a read-only ``Baser`` open of a
    missing database does not fail, it creates an empty one and pollutes the path (~5s3e), so
    "does it open" is not the same question as "is it there".
    """
    for path in candidates(klas, config):
        if os.path.exists(os.path.join(path, _DB_FILE)):
            return path
    return None


def kind(klas):
    """The last segment of a store's tail — ``db``, ``ks``, ``reg``.

    Used as the directory name inside a backup, so the backup mirrors the shape of the keri home
    it will be restored into and the restore stays a directory copy.
    """
    return klas.TailDirPath.rstrip("/").split("/")[-1]


def home(klas, config):
    """The directory that CONTAINS the per-kind store directories, or ``None``.

    Where the config file lives, and the directory a backup is restored into. Derived by walking
    up from a resolved store rather than by recomposing a head and a tail, because the two heads
    disagree about more than the head — the alt tail is ``.keri/db`` where the primary is
    ``keri/db`` — so only the path that actually resolved knows which one it came from.

    A store resolves to ``<home>/<kind>[/<base>]/<name>``, so that is two segments up, or three
    when a base is in play.
    """
    resolved = resolve(klas, config)
    if resolved is None:
        return None
    for _ in range(3 if config.base else 2):
        resolved = os.path.dirname(resolved)
    return resolved
