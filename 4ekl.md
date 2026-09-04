# Measure the query-not-found escrow cost before claiming anything about it (SEC-F1, and docs/deploying.md says the same). Kevery.processQueryNotFound re-walks the whole qnfs table every hio pass; sustaining N entries costs an attacker ~N/300 req/s on the publicly-proxied port, unauthenticated. Bounded by TimeoutQNF=300s so it drains, but the per-pass cost degrades the single-threaded loop the whole design protects. Wanted: a latency-versus-query-rate curve. Until that exists this is a code reading, and per the standing rule nothing goes upstream to WebOfTrust without it. Compensating control (edge rate limiting) lives in infra and is unconfirmed.
kind: todo
created: 2026-09-04T02:05Z

