"""witness — Bakobo's operator layer over a stock keripy witness.

witness makes a running keripy witness observable without forking keripy: a witness runner
(later) plus a separate, read-only control-plane process. This P0 slice is the control-plane
reader — a process that opens the witness's LMDB read-only (so it can never stall or corrupt
the witness) and serves liveness and identity over HTTP.

The design lives in ``this.i`` (the intent tree, source of truth) and ``docs/``.
"""

__version__ = "0.0.0"
