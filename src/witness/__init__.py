"""witness — Bakobo's operator layer over a stock keripy witness.

witness makes a running keripy witness observable without forking keripy (@q4m7tz). It ships two
process roles in one CLI (@g3w6px), co-located in one container (@a24p3kbw): ``witness run``, the
launcher that carries keripy's own witness doers plus in-loop telemetry (@n5r2vq, @vxt7feoi), and
``witness control-plane``, a separate process that reads the witness's LMDB strictly read-only so
it can never stall or corrupt it (@c7v3kp, @k3p7wr).

The design lives in ``this.i`` (the intent tree, source of truth) and ``docs/``.
"""

from importlib.metadata import PackageNotFoundError, version as _installed_version

try:
    #: Read from installed metadata so the package and pyproject.toml cannot disagree, which is
    #: the same drift @lnk24kwp closes between the image and the keripy pin.
    __version__ = _installed_version("witness")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"
