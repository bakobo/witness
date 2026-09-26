# SEC-F1 (2026-09-26 health panel, CONFIRMED by its verifier): the Registrar's sightings table is capped globally (4096) as well as per signer (32), and any AID may subscribe. Throwaway AIDs that subscribe then unsubscribe each leave sightings behind (dodging the 64-subscription cap), so one attacker at ~63 req/s holds the table full for the 65 s keep window and every legitimate subscribe/unsubscribe is refused 429. Needs a design decision, not a one-liner: options include not counting a signer with no live subscription against the global cap, pruning an unsubscribed signer's sightings, or rate-limiting subscription churn per source. Location src/witness/registrar/store.py:166.
kind: todo
created: 2026-09-26T09:21Z

