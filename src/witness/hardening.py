"""The runtime flags every ``docker run`` of the witness image passes (@4hwwhshr).

Held once, here, because three things must agree on them and each would otherwise carry its own
copy: the image oracles, which prove the image works under them; ``witness pool``, which runs the
image for experiments; and docs/deploying.md, which tells an operator what to type (a test holds
the docs to this tuple).

Writable paths under these flags are the data volume, ``/backup`` where it is mounted, and the
``/tmp`` tmpfs below, and nothing else. ``/tmp`` is not optional: hio resolves a temp directory
when it is imported, so without it ``import keri`` fails and nothing starts. The data volume is
not made read-only and must never be: LMDB omits its lock file on a read-only filesystem and its
readers then lose data silently (@a24p3kbw).
"""

RUN_FLAGS = (
    "--read-only",
    "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "128",
)
