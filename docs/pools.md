# Witness pools, for experiments

`witness pool` stands up a throwaway set of witnesses — real containers of this repo's image, each with its own volume, keystore, PID namespace and published ports — and removes all of it again with one command. It is for experiments: what a controller's `toad` buys, what a verifier sees when one witness goes away, what a credential ceremony does against witnesses that genuinely receipt. The design and its rejected alternatives are `this.i` `@n2bgpdds`.

It runs on a **host with Docker**, not inside the witness image. It is the one `witness` subcommand that is not part of a deployment.

## A pool in one command

```sh
export WITNESS_IMAGE=ghcr.io/bakobo/witness@sha256:<digest>   # or a local `docker build -t witness:dev .`
uv run witness pool up --name lab --count 3
```

```
pool lab — 3 witnesses on witness:dev
  w1  BGKVzj4ve0VSd8z_AmvhLg4lqcC_9WYX90k03q-R_Ydo  http://127.0.0.1:5640/  control http://127.0.0.1:5642/
  w2  BBilc4-L3tFUnfM_wJr4S4OJanAv_VmF_dJNN6vkf2Ha  http://127.0.0.1:5650/  control http://127.0.0.1:5652/
  w3  BKY3AGbfayGzBcOScome5Kuk7Ei_DuzbDCFSFT5R5rsE  http://127.0.0.1:5660/  control http://127.0.0.1:5662/
`witness pool manifest --name lab --format heti` describes it; `witness pool down --name lab` removes it.
```

There is deliberately no floating tag to default to (`@lnk24kwp`), so `--image` is required — from the flag, or from `WITNESS_IMAGE`, the same variable the image oracles take.

**Ports.** Witness *i* takes `--base-port + 10(i-1)` for its witness HTTP port, with its control plane two above; the default base is 5640, clear of the 5631/5632/5633 a hand-run witness uses. Everything binds to `127.0.0.1`. keripy's CESR-over-TCP port is not published, because nothing in an experiment needs it and `infra` closes it in production too.

**Cleanup.** `witness pool down --name lab` removes every container and every volume carrying the pool's label, and the pool writes nothing else anywhere — no state file, no run directory. `--all` sweeps every pool on the host. Because cleanup keys on labels rather than on bookkeeping, an `up` interrupted halfway still tears down in full.

## Watching it while it runs

Each witness has its own control plane, unauthenticated on loopback, and it answers throughout:

```sh
curl -s http://127.0.0.1:5642/v1/witness/loop | jq .lag
curl -s http://127.0.0.1:5652/v1/witness/escrow | jq .depths.query_not_found
```

`witness pool status --name lab` asks all of them at once:

```
  w1  running  ok  ticks 1042  controllers 2
  w2  running  ok  ticks 1039  controllers 2
  w3  exited   not running
```

The controller count is the observation a pool exists for: after something incepts against the pool, watching every witness arrive at the same controller — and, through `/v1/witness/controller/<aid>`, at the same sequence number and the same event SAID — is the multi-witness behaviour in one line.

Read it as a delta rather than an absolute. A witness that has never seen a controller already reports **two** key states: its own AID, and a second one keripy's own witness setup incepts (measured 2026-09-11, on a plain container as well as a pooled one; tick `4pqt`). So three witnesses each holding three is one controller witnessed by all of them.

## Taking a witness out of service

```sh
uv run witness pool break --name lab --witness w2            # stopped: connections are refused
uv run witness pool break --name lab --witness w2 --mode pause   # frozen: requests hang instead
uv run witness pool heal  --name lab --witness w2            # reverses whichever it was
```

`stop` is what a witness being down looks like to a client. `pause` freezes the process mid-flight, which is the shape of a witness that has become unreachable without saying so — a different failure, and the one that finds timeout bugs.

## Designating the pool

**The pool never incepts anything** (`@jorbhpfq`). It serves witnesses and describes them; whatever holds keys does the designating. `manifest` emits that description in the form its consumer wants.

For [heti](https://github.com/bakobo/heti), whose lockbox takes a standing witness set:

```sh
uv run witness pool manifest --name lab --format heti >> ~/.config/heti/config.toml
```

```toml
[witnesses]
oobis = [
  "http://127.0.0.1:5640/oobi/BGKVzj4ve0VSd8z_AmvhLg4lqcC_9WYX90k03q-R_Ydo/controller",
  "http://127.0.0.1:5650/oobi/BBilc4-L3tFUnfM_wJr4S4OJanAv_VmF_dJNN6vkf2Ha/controller",
]
toad = 2
```

For `kli`, as an inception document:

```sh
uv run witness pool manifest --name lab --format kli > /tmp/lab-incept.json
kli incept --name alice --alias alice --file /tmp/lab-incept.json
```

And `--format json` (the default) carries every witness with its alias, AID, witness URL, OOBI and control-plane URL, for anything else that needs to read it.

The suggested `toad` is keripy's own `ample(n)` — 2 of 2, 3 of 3, 3 of 4, 4 of 5, 5 of 7 — rather than a number this repo invented. `--toad` overrides it.

## Reproducible pools

By default each witness gets a random salt, so a pool's AIDs are new every time. `--seed <string>` derives them instead, so the same seed gives the same AIDs and the same OOBIs across runs — which is what lets a fixture, a config file or a set of notes outlive the pool they were written against.

```sh
uv run witness pool up --name merti --count 2 --seed merti-email-ceremony
```

The cost is exactly what it sounds like: **a seeded pool's signing keys are derivable by anyone who knows the seed**. That is fine for a laboratory and unacceptable anywhere else, which is the same reason every pool is throwaway.

## What a pool is not

**It is not how you deploy a witness.** That is [`deploying.md`](deploying.md): one container, a persistent volume, a digest, and a reverse proxy. A pool publishes several control planes on loopback with no authentication at all and keeps nothing.

**It is not the cheapest way to get witnesses.** If the question is about the KERI protocol rather than about this image, bakobo/heti runs genuine keripy witnesses in-process, in under a second, with a relay in front of each one so a test can make it slow, absent, refusing or serving garbage. Use that. A pool costs containers, and what it buys is that the witnesses are the artifact `infra` deploys — the supervisor's failure policy, the healthcheck, the escrow pacing in the image's own CMD, and a control plane answering while the experiment runs.

**Its witnesses are not stock keripy.** The image defaults `--escrow-interval` to 1 second and `--escrow-timeout` to 60, where keripy sweeps every hio pass and holds an unanswerable query for 300 (`@zj3h2pzh`, `@znm5uppx`, measured in [`escrow-load.md`](escrow-load.md)). An experiment about keripy's own behaviour should know that.

## How it works, in the two places that matter

**The witness's advertised URL is seeded into the volume before first start.** A witness tells a resolver where to reach it through `curls` in keripy's config file, which keripy reads exactly once — when `makeHab` creates the hab. So the pool writes that file into the volume, using keripy's own `Configer` inside a one-shot container, between `kli init` and the first start. It goes to keripy's default location (`/usr/local/var/keri/cf/witness.json`) rather than arriving through `--config-dir` on an overridden container command, because overriding the command would mean restating the image's `CMD` in the pool's code, where it would drift silently the first time the image's own command changed. `tests/test_pool_oracle.py` resolves a real OOBI to prove the seeding landed, because a witness with no config serves an OOBI too — just one that names no location.

**Nothing is written outside Docker.** The manifest is derived live from the labels and from each control plane's `/v1/witness/identity`, so what the pool reports cannot disagree with what is running, and `down` has nothing to clean up but containers and volumes.
