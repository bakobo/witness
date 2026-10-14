"""Record keripy's accepted event and witness-receipt writes on its loop thread.

The hook has no reads of keripy's database. Its small call surface is frozen by
``tests/test_changehook.py`` under @52dwv9ky.
"""


def _parts(keys):
    """Return the AID and digest from keripy's tuple or joined digest key."""
    if isinstance(keys, tuple):
        aid, digest = keys
    else:
        aid, digest = keys.rsplit(b".", 1)
    return (
        aid.decode() if isinstance(aid, bytes) else aid,
        digest.decode() if isinstance(digest, bytes) else digest,
    )


def _record(log, keys, digest=None, fn=None):
    """A broken side log cannot break the keripy write that already succeeded."""
    try:
        if digest is None:
            aid, digest = _parts(keys)
        else:
            aid = keys.decode() if isinstance(keys, bytes) else keys
            digest = digest.decode() if isinstance(digest, bytes) else digest
        log.append(aid, digest, fn)
    except Exception:
        try:
            log.fail()
        except Exception:
            # Nothing is left to record it in; the runner's unclean exit turns the generation
            # over on the next start (@52dwv9ky). Raising here would fail keripy's write.
            pass


def install(db, log):
    """Wrap only the three keripy stores that record feed-relevant changes."""
    original_fel = db.fels.append
    original_add = db.wigs.add
    original_put = db.wigs.put

    def fel_append(keys, val):
        result = original_fel(keys=keys, val=val)
        # The first accepted event returns ordinal 0, so truthiness is wrong.
        if result is not None:
            _record(log, keys, val, result)
        return result

    def wig_add(keys, val):
        result = original_add(keys=keys, val=val)
        if result:
            _record(log, keys)
        return result

    def wig_put(keys, vals):
        result = original_put(keys=keys, vals=vals)
        if result:
            _record(log, keys)
        return result

    db.fels.append = fel_append
    db.wigs.add = wig_add
    db.wigs.put = wig_put
