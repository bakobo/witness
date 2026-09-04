# P3 auth: the read surface ships unauthenticated (SEC-F2, TST-F2 from the v0.1.0-rc panel). @s6v3qm decided RFC 9421 signed requests via heti; until it lands, the only confinement is the operator's -p 127.0.0.1:5633 publish flag, and a single slip exposes witness identity, all controller key state, escrow/database internals and process vitals. The Dockerfile CMD binds 0.0.0.0 because in-container loopback is unreachable, so the fail-open direction is real rather than theoretical. Also blocks TST-F2: no HTTP-surface test exercises an auth rejection path because there is none to exercise. Required before any mutating endpoint (P4).
kind: todo
created: 2026-09-04T02:05Z

