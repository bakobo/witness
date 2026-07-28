## Starting a repo from this template

*This section documents bootstrapping a **new** repo from `bakobo/template`. Once your repo is set
up, delete this section — like the Testing/CI/README stanzas below, it is self-removing.* Two ways
to start; both end at the same per-clone setup.

**A. Canonical — GitHub template (preferred).** Create the repo straight from the template so the
scaffolding arrives automatically:

```sh
gh repo create bakobo/<name> --template bakobo/template --private
```

**B. Vendor into an existing / hand-made repo.** When the target repo already exists (you ran
`git init` yourself, or you are retrofitting an older repo), copy **only** the template's tracked
scaffolding into it — `AGENTS.md CLAUDE.md GEMINI.md .cursorrules .gitignore this.i.seed .github/`.
Do **not** copy `.git/` or `.tick/` (the tick ledger is per-clone; see below).

**Per-clone setup (run in every fresh clone, both paths):**

1. **`tick init`** — connect the clone to the task ledger (adopts the remote ledger if a colleague
   already made one, else creates it). Not tracked on `main`; it is an orphan `tick` branch plus a
   gitignored `.tick/` store. Once the repo has a remote, `git config tick.remote origin` and push
   the `tick` branch so the ledger is backed up.
2. **Intent (`this.i`).** If anyone will later need to know *why* this repo is built the way it is,
   adopt intent: `cp this.i.seed this.i`, rewrite the root goal to this repo's real purpose (the
   rebuttal-surface standard), give it a fresh opaque id, and delete `this.i.seed`. A pure
   content/asset/config repo may instead just delete `this.i.seed` — its absence is the opt-out.
3. **Docs, README, CI.** Follow the repo-layout convention — design/architecture docs under `docs/`
   (the **Repo layout** rule in the engineering-standards block below;
   [`dev/standards/repo-layout.md`](../dev/standards/repo-layout.md)). Add a `README.md` (fresh-clone
   → passing tests, with a clickable CI badge) and CI once the repo gains code, per the stanzas below.

## Bakobo engineering standards

How every Bakobo repo builds is governed by cross-cutting standards, canonical in the sibling
[`bakobo/dev`](../dev) repo. If `../dev` is not checked out beside this one, clone it before design
work: `git clone --depth 1 https://github.com/bakobo/dev`. Always on:

- **Intent-first** development and **strict TDD at 100% branch coverage of new code** — see the
  sections below and [`dev/methodology.md`](../dev/methodology.md).
- **Fail closed.** Untrusted input never carries authority; when something can't be checked, the
  effect does not land ([`org` principle 8](../org/design/purpose-and-principles.md)).
- **High-quality errors.** Every error carries a stable symbolic code, says whether retrying could
  help (permanent vs. transient), and reads as complete, plain sentences in the house voice — never
  "something went wrong." Full standard: [`dev/standards/error-handling.md`](../dev/standards/error-handling.md).
- **Repo layout.** Architecture and developer docs live in `docs/`; the root holds only repo-level
  files (`README`, `LICENSE`, `CONTRIBUTING`), the instruction/config files, build manifests, and
  `this.i` at the root as the source of truth. Don't leave `design.md` loose at the root. Full
  standard, including the content-repo nuance: [`dev/standards/repo-layout.md`](../dev/standards/repo-layout.md).
- **Terminology.** Bakobo's architecture has a precise vocabulary (`core`, `steward`, `mint`, …). Its
  single source of truth is [`bakobo/glossary`](https://github.com/bakobo/glossary), reached via the
  `glossary` MCP server. Consult a term before using it, reconcile prose to the glossary (not the
  reverse), mint/amend terms in-band through the MCP (never hand-edit), and don't let a general word
  masquerade as a formal term. Full standard: [`dev/standards/terminology.md`](../dev/standards/terminology.md).
- **Tasks and tech debt in `tick`** — see the tick stanza below, not an external tracker.
- **Craftsman working posture.** Development follows the `cc` craftsman methodology — interview at
  intent level, dispatch briefs to worker sub-agents, verify against oracles, and learn from every
  failure. It is Daniel Hardman's personal craft (the private `cc` repo), adopted across Bakobo; the
  operational rules for *this* repo are in [`dev/methodology.md`](../dev/methodology.md).

## Intent methodology

Bakobo develops intent-first. If this repo has design decisions worth explaining, its source of
truth is `this.i` (the intent tree) at the repository root — code and `docs/` are derived from it.
Record each consequential decision in `this.i` **first**, in its own commit, **before** the code
commit it justifies. The full rules — what `this.i` is, when a repo needs one, the speculative
interview, the `why` rebuttal-surface standard, the gate ceremony, and adversarial review — are in
[`dev/methodology.md`](../dev/methodology.md), in the sibling `bakobo/dev` repo. Read it before
making design decisions here.

If this repo has no `this.i` yet and warrants one, see [`dev/methodology.md`](../dev/methodology.md)
§2 and the shipped `this.i.seed`. A trivial repo (pure content/assets/config, where no one will
later need to know *why*) may skip intent entirely — just delete `this.i.seed`.

## Testing Protocol

This repository appears to have no test suite yet. Before writing feature code
or fixing bugs, propose an appropriate testing framework for this project type
and add a single trivially-passing test to confirm the setup works. In the
same commit, propose to change AGENTS.md to replace this instruction with one
that instructs agents to follow strict TDD from now on: write failing tests
that capture happy path and edge cases/unhappy paths for each requirement,
observe them fail, implement until tests pass, never checkin without proving
that all tests pass, aim for 100% of all new code, and always leave existing
code better tested than it was before you touched it.

## CI and Documentation

This repo appears to have no CI workflows yet. Until it does, any time you make
code changes to the user, propose an appropriate set of GitHub actions (e.g.,
`.github/workflows/ci.yml`) that builds and runs tests on every push and
pull request. Propose to remove this instruction from AGENTS.md on the
same commit.

This repository has no README. As long is this is the case, any time you
make code changes for the user, propose to add a `README.md` that explains how
to get from a fresh clone to passing tests, with a clickable CI status
badge at the top for each active workflow. Propose to remove this
instruction from AGENTS.md on the same commit.

When writing or modifying GitHub Actions workflows, always use the latest
stable release of each action. Avoid versions pinned to Node.js 16 or
Node.js 20 (both deprecated by GitHub). In 2026, this meant to prefer Node.js
24-compatible versions, but the standard may evolve over time. Check the GitHub
Marketplace for each action's current release.

<!-- >>> tick stanza >>> (managed by `tick init`) -->

## Task tracking: `tick`

This repo tracks tasks, tech debt, and ideas in a local [`tick`](https://github.com/dhh1128/tick)
ledger (an orphan `tick` branch; the `tick` CLI is the interface). Reads are plain
files — do **not** use an external API for task tracking.

- **First, if a `tick` command says the repo isn't initialized**, run `tick init`
  once to connect this clone to the ledger — it adopts the existing remote ledger
  if a colleague already set one up, or creates a new one otherwise.
- **A tick mark is the sigil `~` immediately followed by a digit-first 4-char
  base32 id** (the id part looks like `4mz3`, so the full mark is that id with a
  leading `~`). It pins a tick to a code location.
- **Before editing a file**, grep it for marks and read what they reference:
  `rg '~[2-7][a-z2-7]{3}\b' <file>` then `tick show <id>`. A mark means recorded
  context exists for that spot — read it first.
- **Search** existing ticks with `tick grep <text>`; **list** with `tick ls`.
- **Capture** new work with `tick add "<title>"` and place the printed mark
  (`~` + the new id) at the relevant code spot.
- When your change **resolves** a tick, run `tick off <id>` and **delete the
  mark(s)** it reports still in the code.

<!-- <<< tick stanza <<< -->
