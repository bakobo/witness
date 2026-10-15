# Making witnesses discoverable

A controller designates a witness by its AID, and finds it by its OOBI. A single witness already serves both: its OOBI at the keripy port, and its AID, tags and attributes at the control plane's open nouns (`identity`, `tags`, `attribs`; see [`control-plane.md`](control-plane.md)). [`witness landing`](landing.md) turns those into a page a person can read at the witness's `/`.

An operator running several witnesses needs one more thing: a place, under its own domain, that lists them. Nothing in this repo produces that, and nothing in KERI requires a particular shape. What follows is the convention Bakobo uses for its own pool, offered as an example to copy rather than as a feature of the image.

## Bakobo's example

[`https://bakobo.com/.well-known/index.html`](https://bakobo.com/.well-known/index.html) is the human entry point. The machine one is three files:

- [`/.well-known/host-meta.json`](https://bakobo.com/.well-known/host-meta.json) — RFC 6415 link relations: a lookup template, `/.well-known/oobi/{said}/index.json`, and a link to the catalog. A client that knows only the domain starts here.
- [`/.well-known/oobi/index.json`](https://bakobo.com/.well-known/oobi/index.json) — the catalog: every OOBI resource the domain publishes, grouped by type, each witness with its name and hostname.
- `/.well-known/oobi/<AID>/index.json` — one witness's OOBI, its signed inception event, resolvable without knowing what kind of resource the AID names.

All three are generated from the estate rather than written by hand, so the catalog cannot drift from the witnesses actually running.

The index page also states something every operator of a pool should state: who runs the witnesses relative to who controls the AIDs they witness. Bakobo operates every witness listed there and also controls the AIDs they witness, so a receipt from that pool is evidence of availability, not of controller-accountability. A validator who needs protection against a controller colluding with its own witnesses must get it from witnesses run by someone else.

## Trying a witness

Bakobo runs a trial witness at [`https://wit1.try.bakobo.com/`](https://wit1.try.bakobo.com/), for evaluating this image against something real. It promises nothing: no uptime, no backup, and a rebuild gives it a new AID, so read its AID from `/v1/witness/identity` each time rather than recording it. Its terms are at [`https://bakobo.com/witness/trial-terms.html`](https://bakobo.com/witness/trial-terms.html). It is not in the `.well-known` catalog above, which lists the production pool only.

For experiments that need several witnesses, or that need to break one, [`witness pool`](pools.md) stands up a throwaway set locally.
