# What a witness declares about itself

> **Status: built, connected, and live for pools.** The control plane serves `/v1/witness/tags` and `/v1/witness/attribs`, prefers a signed declaration when one exists and falls back to operator configuration when it does not, and `controller/{aid}` reports the tags an AID inherits. The signed channel — `/decl/tags` and `/decl/attribs` reply routes, disseminated through OOBI — is implemented in keripy and the pin now carries it, so a witness that has declared serves `signed-reply`. The upstream PR is open rather than merged, and it invites a route name other than `/decl`, so the names here may still move. The decisions are `this.i` `@vqqh6zdk` and its children; this document is derived from them and carries no authority of its own.

*Written 2026-09-16, when the question was where a "this witness is laboratory infrastructure" marker can live in KERI. Two carriers looked right and were measured and rejected, and the measurements are recorded here because `@vqqh6zdk` rests on them — the node states the conclusions, and this is the evidence a challenger would need to disagree with them.*

## The problem

A witness needs a way to say "I am experimental, do not build anything consequential on me," and an AID witnessed by such a witness needs to inherit that. The motivating semantics are COIA's flag `6` — an identifier belonging to a test or demonstration environment, with no real-world consequence to reputation, governance or cost, and cleared by nothing, because a test identifier does not become a production identifier.

Today a laboratory witness and a production witness are indistinguishable to anyone who resolves them, and so are the AIDs they witness.

## Only the negative claim is credible

There is a `testnet` tag and deliberately no `production` tag (`@pmtzkn6j`).

A witness asserting that it is production makes a self-serving claim that nothing verifies — worthless against an adversary and no better than absence. A witness asserting that it is *not* for production speaks against its own interest, which is the only class of self-assertion worth reading. So the derived value is three-valued: tagged, untagged, and never production-asserted. An empty tag list means "nothing claimed", never "this is fine".

This narrows the fail-closed principle rather than contradicting it. Treating "untagged" as "testnet" would taint every AID in existence today, which would make the feature useless within a week of anyone relying on it. Failing closed here means refusing to read absence as a positive signal — not manufacturing a negative one.

## Two kinds of declaration

A **tag** is a bare name a consumer *decides* on. A **attribute** is a key and value a consumer *displays*. That line, not mutability, is what separates them (`@e4ceoopg`).

The decisive reason they cannot be one thing is that union is defined for names and undefined for pairs. Tag inheritance is a union over an AID's witness set: any witness tagged `testnet` taints the AID, monotonically and obviously. Three witnesses reporting `region=eu`, `region=us` and `region=ap` have no natural merge, and every artificial one is a rule somebody has to agree to. So attributes are not inherited at all, and `controller/{aid}` carries derived tags and nothing else.

The second reason is evidential. `testnet` is believable because it is a claim against interest; no valued attribute has that property, so a self-reported region is worth exactly what a self-reported `production` would have been.

Bare tag names and bare attribute keys are a defined vocabulary. Anything with a dotted vendor prefix — `bakobo.pool`, `bakobo.rack` — is anyone's to mint. That split is what keeps an open namespace from collapsing into mutual unintelligibility while still leaving an extension point: a bare name that is not recognized is a typo, and a misspelled `testnet` that silently fails to apply leaves a witness looking production-grade with nothing to say otherwise.

## Why not a keripy configuration trait

A configuration trait in the witness's inception event — a seventh code in `TraitCodex` beside `EstOnly` and `DoNotDelegate` — looks like the right home and is not. Measured against the pinned keripy (`bakobo/keripy@366d810`, keripy 2.0.0-dev6):

**A witness AID's prefix does not commit to its traits.** A witness AID is non-transferable by necessity — a transferable witness would need witnesses of its own and the definition would not terminate, which is the reasoning already recorded at `src/witness/reader.py:80`. A non-transferable `B…` prefix *is* the public key. Incepting one key with and without `cnfg=["NP"]` produced the identical prefix `BB-fH5uto5o5XHZjNN3_W3PdT4MIyTCmQWDzMxMZV2kI` and only a different event SAID. For contrast, the same test on a self-addressing `E…` AID moves the prefix from `EEKF-BG3-hinxIsKi6aE9QDaXJax-z9IGWi0EUXP59vw` to `EE-BvohRXnvKJ9e53mmGwML_boO6A96Fl6lQdYSuPBOR`. Prefix commitment is real, and it is available only to the kind of AID a witness cannot be.

**And the event has no audience.** Verifying a witness receipt needs the prefix alone, so nothing in the ecosystem fetches a witness's KEL. The inception event does exist — `hby.makeHab(transferable=False)` stores one at `db.kels` sn=0, reachable via `hab.iserder`, replaying as 395 bytes of JSON — but it is keripy's own bookkeeping rather than a document anyone reads.

Two further facts make the trait carrier worse than it first appears. `TraitCodex` membership is unenforced on the JSON serialization and enforced on the CESR-native one, where `Traitor` encodes a trait as a codex lookup and an unregistered string has no encoding at all (`InvalidSoftError`, `src/keri/core/coring.py:2530`) — so a private code is mintable today by anything emitting JSON and unmintable by anything emitting native CESR. And `Kever.state()` rebuilds the trait list from two hardcoded booleans (`src/keri/core/eventing.py:3922`), so an unrecognized trait never reaches key state: measured, `state().c == []`.

## Why not an anchor in the inception event

Rejected on first inspection rather than proven shut. Both a bare dict and a `{"d": <said>}` seal in the icp `a` field were refused by Serder validation, on both the JSON and CESR-native paths. The exact seal shape an inception event will accept was not chased down, so this door is closed on inspection and could be reopened by someone who wants to.

## What survives: the channel that already exists for this

A non-transferable AID's degenerate KEL cannot carry facts about the witness — which is precisely why KERI already has a channel for that class of fact. `/loc/scheme` and `/end/role/{action}` are signed `rpy` messages a witness issues about itself, stored under BADA and replayed through its OOBI; `Hab.reconfigure` emits both at startup (`src/keri/app/habbing.py:1203`).

Declarations belong beside them. That gives signature by the witness's own key, carriage over rails that already exist, replay to third parties who never contact the witness directly, and — because BADA orders by timestamp — a change that is visible and ordered rather than silent. Unknown reply routes fail soft on peers that do not know them, where an unregistered trait fails at the wire format.

Tags and attributes get **separate** routes, for the same reason they are separate nouns here: BADA orders each route independently, and tags change almost never while a contact address changes often. One route would mean re-signing and re-timestamping the `testnet` assertion in order to correct a typo in an email address.

## Stickiness lives at the consumer

COIA's flag `6` is cleared by nothing, but no carrier available to a witness can make its own assertion immutable — that is what the measurements above establish. So the monotonicity is enforced where the judgement lives: once a consumer has seen a witness tagged `testnet`, that marking never clears for that consumer, whatever the witness later says (`@dhh2gnvv`).

Letting it clear at the consumer too would let a laboratory witness retroactively launder every AID it ever witnessed, simply by re-tagging itself. The accepted cost is that two consumers can legitimately disagree about the same witness — COIA's creator-indexed class — and that a consumer with no history sees only today's claim. `witness.decls.derive` is therefore a pure function, and the caller owns persistence.

## Partial answers are first-class

`controller/{aid}` reports `derived`, `from` and `unresolved`. The last is a member rather than an omission, because the control plane never calls out to peer witnesses: it is a read-only observer (`@k3p7wr`), and an outbound fetch would add an SSRF surface and a network dependency to the one process whose whole charter is not having one. A co-witness's tags are genuinely unknown to it, and saying so beats guessing. A caller who wants the whole picture asks each witness itself.

## What is not built

- **Nothing declares automatically outside a pool.** A production witness says nothing about itself until an operator gives it `--tag`, `--attrib` or a seed file, which is correct: absence is not assurance, and a witness that declared something by default would be declaring something nobody chose.
- **Delegation** (`~4tml`). Inheritance walks an AID's own witness list and stops. Whether a delegated AID inherits from its delegator's witnesses is undecided.
- **A door census** (`~4v2j`). The declaration doors are bounded; nothing yet proves the repo's set of doors is complete.
