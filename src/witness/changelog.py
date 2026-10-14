"""An append-only change log in its own LMDB environment (@6y23f6w2)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import fiki
import lmdb
from keri.db import basing

from . import paths

ZERO_CHAIN = bytes(16)
#: Virtual, not allocated: the file grows with use. At ~150 bytes an entry this holds tens of
#: millions of entries; lmdb's own 10 MiB default would fill after ~60,000 and stop the feed.
MAP_SIZE = 4 * 1024 ** 3


def change_path(config):
    """Resolve the log beside keripy's database without creating either store."""
    probe = SimpleNamespace(name=config.name, base=config.base,
                            head_dir_path=getattr(config, "head_dir_path", None))
    home = paths.home(basing.Baser, probe)
    if home is None:
        return None
    return Path(home) / "change" / config.base / config.name


def _pack(aid, digest, chain):
    return json.dumps([aid, digest, chain.hex()], separators=(",", ":")).encode()


def _unpack(value):
    aid, digest, chain = json.loads(value)
    return aid, digest, bytes.fromhex(chain)


class ChangeLog:
    """The runner's sole writer of a globally ordered log."""

    def __init__(self, env, entries, meta, generation, seed):
        self.env = env
        self.entries = entries
        self.meta = meta
        self.generation = generation
        self.seed = seed
        self.failed = False

    @classmethod
    def open(cls, path):
        path = Path(path)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        env = lmdb.open(str(path), max_dbs=2, map_size=MAP_SIZE, metasync=False)
        entries = env.open_db(b"entries")
        meta = env.open_db(b"meta")
        with env.begin(write=True) as txn:
            generation = txn.get(b"generation", db=meta)
            seed = txn.get(b"seed", db=meta)
            if generation is None or txn.get(b"clean", db=meta) is None or txn.get(b"failed", db=meta):
                generation = os.urandom(16).hex().encode()
                txn.put(b"generation", generation, db=meta)
                txn.delete(b"failed", db=meta)
            if seed is None:
                seed = fiki.Key.generate().seed
                txn.put(b"seed", seed, db=meta)
            txn.delete(b"clean", db=meta)
        return cls(env, entries, meta, generation.decode(), seed)

    def append(self, aid, digest):
        if self.failed:
            raise RuntimeError("The change log is failed; it cannot accept entries.")
        if isinstance(aid, bytes):
            aid = aid.decode()
        if isinstance(digest, bytes):
            digest = digest.decode()
        with self.env.begin(write=True) as txn:
            cursor = txn.cursor(db=self.entries)
            if cursor.last():
                ordinal = int.from_bytes(cursor.key(), "big") + 1
                previous = _unpack(cursor.value())[2]
            else:
                ordinal = 1
                previous = ZERO_CHAIN
            chain = hashlib.blake2b(previous + aid.encode() + b"\x00" + digest.encode(), digest_size=16).digest()
            txn.put(ordinal.to_bytes(8, "big"), _pack(aid, digest, chain), db=self.entries)
        return ordinal

    def fail(self):
        """Turn the generation over at once, so no cursor spans the entry just lost.

        If even that cannot be written, writes stop and the next open turns it over instead; a
        log that cannot record its own failure falls quiet until the runner restarts unclean.
        """
        generation = os.urandom(16).hex().encode()
        try:
            with self.env.begin(write=True) as txn:
                txn.put(b"generation", generation, db=self.meta)
                txn.delete(b"failed", db=self.meta)
        except lmdb.Error:
            self.failed = True
            with self.env.begin(write=True) as txn:
                txn.put(b"failed", b"1", db=self.meta)
            return
        self.generation = generation.decode()

    def close(self):
        self.env.close()

    def close_clean(self):
        if not self.failed:
            self.env.sync()
            with self.env.begin(write=True) as txn:
                txn.put(b"clean", b"1", db=self.meta)
            self.env.sync()
        self.close()


class LogReader:
    """One read-only LMDB handle, owned by a single feed request."""

    def __init__(self, path):
        if path is None or not (Path(path) / "data.mdb").is_file():
            raise FileNotFoundError("The change log has not been created yet.")
        self.env = lmdb.open(str(path), readonly=True, lock=True, max_dbs=2)
        self.entries = self.env.open_db(b"entries")
        self.meta = self.env.open_db(b"meta")
        with self.env.begin() as txn:
            generation = txn.get(b"generation", db=self.meta)
            seed = txn.get(b"seed", db=self.meta)
            failed = txn.get(b"failed", db=self.meta)
        if generation is None or seed is None or failed is not None:
            self.close()
            raise RuntimeError("The change log is unavailable until the writer reopens it.")
        self.generation = generation.decode()
        self.seed = seed

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.env.close()

    def last(self):
        with self.env.begin() as txn:
            cursor = txn.cursor(db=self.entries)
            if cursor.last():
                return int.from_bytes(cursor.key(), "big"), _unpack(cursor.value())[2]
            return 0, ZERO_CHAIN

    def chain_at(self, ordinal):
        if ordinal == 0:
            return ZERO_CHAIN
        with self.env.begin() as txn:
            value = txn.get(ordinal.to_bytes(8, "big"), db=self.entries)
        return None if value is None else _unpack(value)[2]

    def entries_after(self, ordinal):
        with self.env.begin() as txn:
            cursor = txn.cursor(db=self.entries)
            if cursor.set_range((ordinal + 1).to_bytes(8, "big")):
                for key, value in cursor:
                    aid, digest, chain = _unpack(value)
                    yield int.from_bytes(key, "big"), aid, digest, chain
