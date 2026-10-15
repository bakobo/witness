# Documentation

## Operating a witness

Read these in order if you are deploying one.

- [`deploying.md`](deploying.md) — the image's contract: what to pull, how to verify it, what to run, the three mistakes that are silent, health and alerting, upgrading, backup and restore.
- [`../deploy/`](../deploy/README.md) — a reference kit for a public witness: hardened compose file, Caddy, nftables and fail2ban, with how to install each and prove it is live.
- [`hardening.md`](hardening.md) — what has been hardened, what proves it, and what each protection does not cover.
- [`control-plane.md`](control-plane.md) — the HTTP surface: routes, the change feed, tags and attributes, metrics, errors, and who may read what.
- [`discovery.md`](discovery.md) — publishing a set of witnesses under your domain, with Bakobo's `.well-known` as the example, and a trial witness to try.

## Other tools in the image

- [`pools.md`](pools.md) — `witness pool`, a throwaway set of witnesses for experiments.
- [`landing.md`](landing.md) — `witness landing`, a static page for a witness's `/`.
- [`registrar.md`](registrar.md) — `witness registrar`, which publishes an issuer's TEL heads to verifiers' Observers.

## Design notes

- [`decls.md`](decls.md) — why a witness's self-declarations take the shape they do, and the carriers that were rejected.
- [`escrow-load.md`](escrow-load.md) — what a flood of unanswerable queries costs a witness, measured.

The decisions themselves, and their rationale, are in [`this.i`](../this.i) at the repository root, which is the source of truth these documents are derived from.
