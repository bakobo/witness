# witness — Intent Tree (this.i)
#
# Source of truth for this repo's intentions and the decisions that follow. Code and docs/ are
# derived from it. Format: dhh1128/intent node tree; see bakobo/dev/methodology.md.
# Node key line:  Name = [marks...] type:   (types: goal | decision | constraint | tension | deviation)
# id: opaque base32 [a-z2-7]{6,12}, never a semantic label.  why: meets the rebuttal-surface standard.

Operator layer over a stock keripy witness = goal:
  id: q4m7tz
  why: >
    witness exists to make a running keripy witness observable and governable by Bakobo operators
    WITHOUT forking keripy. Rejected two alternatives: folding these features into a keripy fork
    (we would re-merge upstream forever), and a from-scratch witness reimplementation (reinvents a
    battle-tested component). Accepted tradeoff: two moving parts (a witness runner and a control
    plane) instead of one binary.
  children:

    Never degrade the served witness = constraint:
      id: c7v3kp
      why: >
        The witness is the production-critical component; the operator layer must be unable to
        stall, block, or crash it. This is the driving constraint behind the process split below —
        keripy runs a single-threaded cooperative hio loop where any in-loop handler that blocks
        halts event ingest and receipting.
      children:

        Two processes  runner plus a separate read-only control plane = decision:
          id: k3p7wr
          why: >
            The control plane runs in its own OS process and reads keripy's LMDB read-only, so it
            physically cannot stall the witness's hio loop or corrupt its data. Rejected an
            in-process falcon sidecar mounted on the same Doist (a slow control handler would stall
            the witness) and a pure external bolt-on (cannot ever control the process). Accepted
            tradeoff: state-changing control must cross an IPC boundary instead of a direct call.
            LMDB multi-reader concurrency (readonly env open) makes cross-process reads safe.

    This repo runs the witness via a keripy-library launcher = decision:
      id: n5r2vq
      why: >
        To make the witness controllable later without forking keripy, this repo ships the launch
        entrypoint: it imports keripy as a library, starts the stock setupWitness doers unchanged,
        and (in a later phase) adds one control-agent doer that exposes an IPC control socket on the
        loop. Rejected requiring stock `kli witness start` (leaves no seam for control) and forking
        keripy to embed control (upstream drift). Accepted tradeoff: operators adopt our entrypoint
        instead of kli.

    keripy stays an unforked upstream-tracking dependency = decision:
      id: w7c4mz
      why: >
        Bakobo absorbs upstream keripy on its own schedule through a version pin rather than by
        diverging code; bakobo/keripy was just fast-forwarded to WebOfTrust main to set that
        baseline. Rejected a maintained fork (perpetual re-merge cost). Accepted tradeoff: we ride
        some semi-internal keripy accessors (db.fels, db.clonePreIter), so a commit pin plus
        contract tests guard against drift.
      children:

        Share one keripy commit pin with heti at the current commit = decision:
          id: f5j3wc
          why: >
            witness depends on keripy directly AND on heti, which itself pins keri to a git commit;
            two different direct git refs for `keri` cannot co-resolve, so both repos must share ONE
            pin. Chose the current commit (366d8107) over heti's older de59bc7d because the reconciled
            baseline is current-with-upstream and we build against it — accepting a small gated bump
            to heti's pin, gated on re-running heti's 100%-branch-coverage suite against 366d8107 as
            the oracle. Resolved 2026-07-27: heti bumped (heti@2c4247c), suite green (57 passed,
            100% branch). witness pins keri@366d8107 to match.

    v1 is read-only audit and inspection = decision:
      id: t3k6ps
      why: >
        v1 delivers the auditability win first with the lowest risk: the isolated reader process
        needs no channel into the witness at all. Rejected building the control channel and mutating
        endpoints in v1 (touches the single-threaded loop early and forces hardened auth up front).
        Accepted tradeoff: pause/ban/config-reload wait for a later phase; the runner+agent seam is
        designed now but ships inert.
      children:

        Control plane is a stateless projection of keripy LMDB = decision:
          id: b2y5nr
          why: >
            Everything v1 serves (AIDs, key state, KELs, receipts, first-seen timeline via db.dtss)
            is already durable in keripy's LMDB, so maintaining our own datastore would be premature
            (rule of three). Revisit when a concrete feature needs it (metrics-over-time, or the
            future changes-since-root sync index). Accepted tradeoff: no independent or tamper-evident
            audit store yet.

        Use falcon for the v1 control-plane HTTP surface = decision:
          id: r3v6np
          why: >
            The read-only v1 surface is a handful of GET endpoints served by a separate process off
            keripy's hio loop; falcon is already a transitive dependency (via keripy), sync WSGI fits
            synchronous LMDB reads, and it adds no new stack. Rejected FastAPI/Starlette for v1
            because its real advantages — auto OpenAPI, pydantic validation, async — accrue only to
            phases not yet committed (a web console; mutating POST bodies at P4), so adopting it now
            is speculative generality (rule of three, directive #8). Accepted tradeoff: OpenAPI and
            request validation are hand-rolled if needed in v1. Re-evaluation trigger recorded as
            tick ~6cy6: revisit FastAPI (possibly for the UI/mutation surface only, since the control
            plane is its own process) when the web-console or mutation phase makes those needs
            concrete.

        P0 serves GET /healthz and GET /info  unauthenticated = decision:
          id: h5n2rk
          why: >
            /healthz (200 {"status":"ok"}, 503 when the witness LMDB will not open) is a liveness
            probe and must be unauthenticated; /info returns low-sensitivity witness identity
            ({aid, alias, keripy_version, db_path}). RFC 9421 auth (@s6v3qm) is deferred to P1,
            landing on the first endpoint that exposes witnessed KEL/receipt data. Rejected
            authenticating P0 because it forces the heti request-signing path before any sensitive
            data is served, and liveness probes cannot sign. Accepted tradeoff: the identity fields
            in /info are readable without auth. These response shapes are frozen external contracts
            once shipped; changing them needs a new node.

    Authenticate with RFC 9421 message signatures via heti = decision:
      id: s6v3qm
      why: >
        The control plane authenticates each request with an RFC 9421 HTTP Message Signature: the
        caller proves a non-transferable AID, which is stronger than a shared bearer secret and fits
        the KERI posture. Reuses heti.l0.verify_request (KERI-flavor RFC 9421) rather than
        reimplementing 9421 or using bearer tokens (reuse-before-build). Authorization is an
        operator-AID allowlist. Accepted tradeoff: callers must sign requests (heti / signify-ts
        clients), a higher bar than presenting a token.

    Implement in Python to reuse keripy and heti = decision:
      id: d4h7kt
      why: >
        The control plane reuses keripy's own DB accessors and CESR/Serder to read and render
        witnessed state, and the auth dependency heti is Python; a different language would
        reimplement keripy's LMDB schema and CESR parsing. Driving constraint: Python >=3.14 (the
        keripy and heti floor).

    One witness CLI with subcommands per process role = decision:
      id: g3w6px
      why: >
        A single installable `witness` package exposes one CLI with subcommands — `witness
        control-plane` (the reader, P0) and later `witness run` (the launcher) — mirroring keripy's
        own `kli <subcommand>` model, so operators install and discover one entry point. Rejected
        separate console scripts per process (`witness-cp`, `witness-run`), which fragment discovery
        and docs. The two processes stay independently deployable (separate invocations / containers);
        sharing a CLI package does not couple their runtime. Accepted tradeoff: one package ships
        both roles even on a host that runs only one.
