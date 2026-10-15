# The control plane

`witness control-plane` is a separate process that opens the witness's LMDB strictly read-only and serves an HTTP surface over it. This page is its reference: the routes, the change feed, what a witness declares about itself, health, metrics, errors, and who may read what.

## Running it

The image starts it for you. Outside the image, both processes can be run directly against the same keri home:

```sh
witness run --name witness --alias witness --http 5631 --tcp 5632
witness control-plane --name witness --host 127.0.0.1 --port 5633
```

Add `--no-telemetry` to the control plane when the witness was started by stock `kli witness start`, which publishes none.

## The surface

Every path is `/v1/witness/<noun>`, and all are `GET`. Five answer anyone, six require an operator, and the change feed requires a separately granted watcher — see [Authentication](#authentication).

| Path | What it answers | Signed? |
| --- | --- | --- |
| `/v1/witness/health` | Whether the witness is *working*: the database opens **and** the hio loop is not stuck inside a doer. | No |
| `/v1/witness/identity` | The witness's own AID and alias, plus the change feed signing key's AID for pinning. | No |
| `/v1/witness/version` | This package and the keripy it wraps. | No |
| `/v1/witness/loop` | Loop ticks, lag against the tock, the doer currently executing, and per-doer timings. | Yes |
| `/v1/witness/escrow` | Depth of every escrow store, query-not-found first. | Yes |
| `/v1/witness/database` | Size against keripy's fixed 100 MB map ceiling, and the registered reader count. | Yes |
| `/v1/witness/process` | CPU, memory, threads and descriptors for the witness process. | Yes |
| `/v1/witness/tags` | What this witness claims about itself — `testnet`, and any vendor-prefixed tag its operator set. | No |
| `/v1/witness/attribs` | Key/value facts its operator publishes for a person to read: `operator`, `contact`, `pool`, and `terms` and `registration`, which must be https URLs. | No |
| `/v1/witness/controller` | Every controller whose key state this witness holds. | Yes |
| `/v1/witness/controller/{aid}` | One controller's key state and the tags it inherits, or `404`. | Yes |
| `/v1/witness/change?since=<cursor>&limit=<n>` | A page of AIDs changed since a cursor, with CESR replay. | Watcher |

## The change feed

The change feed answers `{"generation", "next", "more", "change": [{"aid", "fn", "kel"}]}`. `kel` is base64 of keripy's genus-pinned replay from first-seen ordinal `fn`, including signatures and receipts held at answer time. Omit `since` to begin an initial sync of every AID from `fn` zero; use `next` to continue the sync and then poll. The first page records the log position before copying any KEL, so changes made during the copy appear again in the first poll. Pages hold at most 1,000 AIDs (`limit` defaults to 100) and 16 MiB of raw replay bytes. A larger page gets `413 e.input.range.change.f`; lower `limit` to try a smaller page. An individual AID above 16 MiB cannot be served by this route yet (~5sqv).

The runner appends one entry to a separate LMDB change log for each accepted event or stored witness receipt. A receipt on an older event therefore triggers a change. The control plane reads the log on demand; a quiet poll needs only the last log position. A cursor records a generation, an ordinal, and that ordinal's hash-chain value. A cursor from another generation, past the end of the log, or with a mismatched chain gets `409 e.state.conflict.cursor.f`; restart without `since`. A clean runner restart keeps the generation. An unclean exit or failed log write changes it, while the chain detects a restored log whose entries have diverged. The log is not pruned, so a watcher can resume from an old cursor while its generation and chain still match.

## Tags and attributes

Tags are worth a paragraph, because what they do *not* say is the point. There is a `testnet` tag and deliberately no `production` tag: a witness claiming to be production makes a self-serving claim nothing verifies, whereas one claiming to be a laboratory speaks against its own interest. So an empty tag list means "nothing claimed", never "this is fine", and a reader must not treat absence as assurance. Set them with `--tag`, repeatable; an undefined bare name is refused at startup, because a misspelled `testnet` that silently fails to apply leaves the witness looking production-grade with nothing to say otherwise. A tag of your own needs a vendor prefix, as in `bakobo.pool`.

Attributes are the other half, and the split matters. A tag is a predicate a consumer *decides* on; an attribute is a key and value a consumer *displays*. That is why they are two nouns rather than one document, and why only tags are inherited. Union is well defined for names — any witness tagged `testnet` taints the AID — and undefined for pairs, since three witnesses reporting three different regions have no natural merge. An attribute also lacks the one property that makes `testnet` credible: `testnet` is a claim against the witness's own interest, whereas a self-reported operator name is worth exactly what a self-reported `production` would have been. So act on tags, show attributes to a human, and trust neither further than that.

Set them with `--attrib key=value`, repeatable. Keys obey the same rule as tag names, so a key is as interoperable as a tag. Values are printable ASCII, non-empty, at most 256 characters — deliberately not Unicode, because this is a string a person reads off a screen, which makes a Cyrillic homograph or a bidi override a spoofing surface rather than an internationalization win.

Both endpoints report a `source`, naming which of three places the answer came from. A witness that has published a signed declaration reports `signed-reply` and serves what it published. Failing that, `--tag` and `--attrib` give `operator-config`. Failing that, a JSON seed file in the volume gives `seed-file` — that is how `witness pool` marks its witnesses, since it will not override the image's command to pass them as arguments. Signed wins, because a third party can verify it and cannot verify a local flag, and letting a flag mask what the witness has already said on the wire would hide the disagreement rather than resolve it. Both endpoints keep answering when the witness database cannot be opened, since "is this the laboratory box?" is a question operators ask precisely when something is broken.

The full reasoning, including the two carriers that were measured and rejected before this one, is in [`decls.md`](decls.md).

An AID inherits tags from its witnesses — and, if it is delegated, from every level of its delegation chain, since a delegate's authority is rooted in its delegator. `controller/{aid}` reports that under `tags`: `derived` is the union, `from` lists the witnesses this control plane can speak for, `unresolved` lists the ones it cannot, and `unfollowed` names delegators whose key state this witness does not hold. The last two are different problems — an unresolved witness is somebody to go and ask, where an unfollowed delegator means there may be witness sets nobody here has seen. That last member is not an omission. This control plane never calls out to peer witnesses — it reads one LMDB and nothing else — so a co-witness's tags are genuinely unknown to it, and saying so beats guessing. A caller who wants the whole picture asks each witness itself. The rule for combining them is monotone: one `testnet` witness is enough, no quorum of untagged ones excuses it, and a consumer that has once seen a witness tagged `testnet` should never clear that marking.

## Health

A witness whose loop has wedged still has a perfectly openable database, so a probe that only opens the database stays green through the failure that has actually happened. This one reports `degraded` and names the doer, but only once that doer has held the loop past five seconds: a sample catching the loop mid-doer is normal, and treating it as a wedge would make the probe a random alarm. What to alert on, and what health deliberately does not report, is in [`deploying.md`](deploying.md#health-and-alerting).

## Metrics

The control plane exports OpenTelemetry metrics over OTLP when a collector endpoint is configured, and does nothing at all when one is not:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 witness control-plane ...
```

Series cover witness liveness, loop lag and per-doer maxima, escrow depth per store, database usage, process vitals, and control-plane request counts by route template and status. There is no scrape endpoint, deliberately: `bakobo/dev`'s `ops.md` §7 puts the OpenTelemetry SDK in code Bakobo writes and OTLP on the wire.

## Errors

Failures are RFC 9457 problem documents with media type `application/problem+json`:

```json
{
  "code": "e.env.witnessdb.unavailable.r",
  "type": "https://errors.bakobo.com/e.env.witnessdb.unavailable.r",
  "title": "I could not open the witness database.",
  "detail": "The witness database was not found at the configured location; the witness may not be running yet.",
  "instance": "/v1/witness/identity",
  "request_id": "…"
}
```

The code carries the meaning: it is classified by what the obstacle was rather than by which component raised it, and the trailing token is the disposition — `.r` means retrying could help, `.f` means it will not. The HTTP status follows from the code's prefix, so two endpoints can never disagree about the same condition.

## Authentication

Six nouns — `loop`, `escrow`, `database`, `process`, `controller` and `controller/{aid}` — answer only a request signed with an RFC 9421 HTTP Message Signature, via [`fiki`](https://github.com/bakobo/fiki), by an operator this witness was told about. The other five stay open: a liveness probe cannot sign, a witness's identity is already public through its OOBI, and tags and attributes are what it broadcasts about itself. `@34sgjmxr` in `this.i` has the reasoning.

Name operators with `--operator <AID>`, repeatable, or with `WITNESS_OPERATORS` (comma-separated), which is read only when no flag is given and is how a deployment reaches into the image's fixed command. An operator's AID is a non-transferable one, the Ed25519 key itself. **With no operator named, the six refuse everyone** with `403 e.grant.missing.operator.f`, and the five keep serving, so upgrading an image without setting it costs those reads and nothing else. A missing, stale or wrong signature is `401 e.id.invalid.control.f`.

Name feed readers with `--watcher <AID>` or `WITNESS_WATCHERS` (comma-separated). An operator has no implicit watcher grant, and a watcher has no implicit operator grant. With no watcher named, the feed refuses everyone with `403 e.grant.missing.watcher.f`; the other nouns keep their existing grants. Feed responses, including refusals, carry an RFC 9421 signature bound to the request. Pin the `change_key` AID from `/v1/witness/identity` before trusting one; it is a separate, persistent non-transferable key stored in the change log because the control plane does not hold the passcode needed to use an encrypted witness key. A new log creates a new key that watchers must pin again.

`witness get` is the client, since curl cannot sign:

```sh
ssh-keygen -t ed25519 -N '' -f ~/.witness-operator      # a key used for nothing else
witness get --key ~/.witness-operator --aid             # the AID to give --operator
witness get --key ~/.witness-operator http://127.0.0.1:5633/v1/witness/escrow | jq .total
```

A signature is good for `--max-age` seconds (default 60) and there is no replay store, because a replayed read changes nothing and discloses only what the captured request already would have. `--authority host:port`, repeatable, also refuses a signature made for some other address; it is off by default, because through an SSH tunnel the address a caller signs is the tunnel's local end, which the witness cannot know. Without it, a request signed for one witness can be replayed to a sibling that names the same operator, inside the window.

Signing does not change where the port belongs. Keep it on loopback, reached by tunnel; authentication is what makes a slip survivable, not a reason to publish it. The one exception is the change feed and identity, which a watcher on someone else's network cannot tunnel to: the deploy kit's Caddy proxies those two routes, GET only, and refuses the rest. A watcher signs the public URL, `https://<witness host>/v1/witness/change`, and the signature verifies behind the proxy because Caddy passes `Host` through unchanged (`@9y806dye`).
