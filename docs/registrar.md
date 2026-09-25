# The Registrar

`witness registrar` runs an issuer's **Registrar**: the issuer-side half of ACDC revocation without phone-home. The issuer publishes each settled TEL head here; verifiers run an **Observer** that subscribes once and receives batches. A verifier then checks a credential's state against its own Observer, never against the issuer and never against this Registrar at verification time. The decisions are in [`this.i`](../this.i), under @r3aonvlz.

No specification defines this protocol. keripy's `registraring.py` is a docstring stub and `keri-foundation/registrar` is a placeholder, so everything below is **ours**, built first for the SEDI Summit demo. Treat it as a proposal, not a standard.

## Running it

```sh
witness registrar --store /var/lib/registrar --port 5700 --publisher B<publisher-aid> [--window SECONDS]
```

On start it prints one JSON line, `{"aid": "B…", "url": "http://host:port"}`. The `aid` is the Registrar's own Ed25519 key, created once in the store and kept across restarts; Observers pin it.

`--window` is the batch window. The default is fifteen minutes, sized for herd privacy (tick `2db3` will choose the production value). A demo on a stage overrides it with seconds.

Only one Registrar may use a store at a time, however its database file is reached (the lock is on the file itself, through any symlink); a second one refuses to start. The directory and its database are owner-only, because they hold the Registrar's signing seed.

Callbacks are untrusted, since any signer may subscribe. By default a callback must resolve to a public address; `--allow-callback` (repeatable) permits a host name or CIDR besides, such as `127.0.0.0/8` for an Observer on the same host. The address checked is the address connected to, and redirects are never followed. `--max-subscriptions` (default 64) caps subscriptions in total; each signer has at most one. `--delivery-timeout` is one wall-clock deadline on each whole delivery, from resolving the host to reading the status (default: ten seconds, or a quarter of the window if that is shorter, and never more than half the window); a delivery still running at the deadline is abandoned. No new delivery starts to a callback whose previous one is still in flight (that window's number is spent), and at most 64 delivery threads are alive at once. Deliveries run concurrently, so one slow callback cannot delay the others.

## The surface

Every request is RFC 9421-signed with [fiki](https://github.com/bakobo/fiki), with the body's `content-digest` covered. There is no GET route: nothing can be pulled, so no verification can cause Registrar traffic.

| request | who | body | answer |
|---|---|---|---|
| `POST /v1/registrar/publication` | a configured `--publisher` | `{registry, issuer, digest, chain, kel}`, with `chain` and `kel` base64 | `{"digest": …}` |
| `POST /v1/registrar/subscription` | any signer; its AID is the subscription | `{"callback": "http(s)://…", "nonce": "…"}` | `{"subscriber": …, "registrar": …}` |
| `DELETE /v1/registrar/subscription` | the subscriber | none | 204 |

The `nonce` is 16 to 64 base64url characters the Observer chooses at random for this subscription; every batch sent for it carries the nonce, and a subscription without one is refused. Subscribing again, with the same callback or a new one, continues the existing sequence rather than restarting it, and replaces the nonce. A signed subscribe or unsubscribe that has already been acted on is refused while it is still fresh (409 `e.state.conflict.registrar.replay.f`); requests are compared by signer, created time and signature bytes, so changing the unsigned label does not make a new request. A request is remembered only if it acted; a refused one can be retried. At most 32 requests per signer and 4096 in all are remembered at a time (429 `e.grant.quota.requests.r` beyond that), and a callback host that does not resolve within five seconds is refused with 503 `e.env.resolver.timeout.r`. Publications are exempt, because a replayed publication changes nothing.

A publication is kept only if its `chain` extends the head already held, byte for byte. An exact repeat is acknowledged again. An older chain is refused with 409 `e.state.conflict.registrar.stale.f`, and a chain that neither extends nor repeats the head with 409 `e.state.conflict.registrar.fork.f`. The Registrar does not verify anchors against the issuer's KEL; the Observer does, and must not rely on the Registrar's word.

## Batches

Every window, each subscriber is sent **one** batch, a JSON POST to its callback, signed by the Registrar's key:

```json
{"registrar": "B…", "subscription": "<the subscription's nonce>", "number": 7, "full": false, "window_end": "2026-11-17T10:00:05+00:00",
 "heads": [{"registry": "E…", "issuer": "E…", "digest": "…", "chain": "<base64>", "kel": "<base64>"}]}
```

- `number` starts at 1 for each subscription and increases by one per window. A batch that is not delivered still uses its number, so a subscriber sees a gap rather than a silently skipped window.
- The first batch of a subscription is `full`: it carries every head. Later batches carry only the registries whose head changed in the window, and a quiet window sends an empty batch. The empty batch is the heartbeat an Observer measures freshness from; it is a temporary demo transport (tick `7nq3`) that a webhook, SSE or better will replace.
- An Observer that sees a gap unsubscribes and subscribes again under a new nonce, which starts a new sequence with a full batch. It refuses any batch whose `subscription` is not its current nonce, so a signed batch captured from an earlier subscription cannot be replayed to close the gap.
