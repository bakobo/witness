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
          children:

            Both processes share one container and one PID namespace = decision:
              id: a24p3kbw
              why: >
                Containerizing creates a concurrency hazard the current deployment does not have,
                and this node is how it is contained. Two systemd units on one host share the host's
                PID namespace, so nothing below arises; putting the two processes in SEPARATE
                containers is what breaks it. LMDB's reader-registration protocol takes an fcntl
                byte-range lock at offset = the reader's own pid, which assumes a single PID
                namespace. Two containers each run their process as pid 1, so the control plane
                collides with the runner and the environment fails to open at all (measured:
                mdb_txn_begin EAGAIN, deterministic, because pid 1 is a convention rather than a
                coincidence). Mounting the LMDB read-only appears to fix this and instead makes it
                far worse: LMDB silently omits the lock file on a read-only filesystem (mdb.c
                mdb_env_setup_locks), the reader stops publishing its snapshot id, and the writer
                recycles pages underneath it — measured, a reader lost all 2000 keys it had just
                read, inside its own transaction, with no error raised and env.flags() still
                reporting lock:True. So the isolation two containers appeared to buy is not
                available: they must share a PID namespace regardless, which already dissolves
                process isolation, and the cgroups share one page-cache pool the kernel declines to
                apportion (measured: a reader scanning 200 MB grew RSS by 200 MB and its cgroup
                charge by 0.5 MB, the writer carrying all of it). Rejected two containers with
                --pid=container:<runner> plus per-file mounts (directory read-only, lock.mdb
                read-write): it does work — measured, the reader registers and a 25 s transaction
                survives churn — but it couples the containers' lifecycles into a systemd ordering
                dependency and buys only a kernel-enforced read-only mount that MDB_RDONLY, which
                reader.py already passes, provides at the library level. Decisive against two
                containers: a control plane that ever starts or stops the runner would otherwise
                need the Docker socket, which is root-equivalent on the host — a far worse posture
                than two processes in one container. This qualifies this node's parent claim that a
                readonly env open makes cross-process reads safe: it does, but only with the lock
                file writable and the pids distinct. Accepted tradeoff: read-only becomes
                LMDB-enforced rather than kernel-enforced; @g3w6px's "separate invocations /
                containers" parenthetical no longer describes the deployment; the image needs a
                supervisor for two processes; and infra's @tdd36d premise that the control plane
                can be added to a running witness without redeploying it acquires a condition —
                true across systemd units, false across containers.
              children:

                The smoke test asserts the reader is registered = constraint:
                  id: v27j7uvo
                  why: >
                    A misconfigured reader is silent: it opens, answers, and returns wrong data only
                    under concurrent writes, so a smoke test that checks "the image builds, starts,
                    and answers health" passes on a broken configuration. num_readers >= 1, as
                    reported by the control plane's own LMDB environment, is the single number that
                    distinguishes the two, because a reader with no lock file reports 0 while the
                    writer is live. Measured: the realistic profile — open, short read, close — ran
                    11,156 iterations against a churning writer with zero anomalies even while
                    unlocked, which is exactly why this would otherwise have shipped.

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

        The whole control-plane surface conforms to the org standards  including what shipped = decision:
          id: zzbdxa
          why: >
            Every control-plane route conforms to bakobo/dev's url-design, http-errors, error-codes
            and error-handling standards, and that includes /healthz and /info, whose shapes
            @h5n2rk froze. This is the new node @h5n2rk requires to change them. All four standards
            were written after @h5n2rk and each claims every Bakobo HTTP surface, so the freeze and
            the standards cannot both stand. Chose to re-cut rather than grandfather because
            witness is private, at 0.0.0, with no external consumer, so the freeze protects nobody
            today and protects more with every consumer it acquires — and because two endpoints of
            one 130-line service currently disagree about where an error lives (/healthz nests it
            under "error", /info does not), which is the sibling-inconsistency error-handling.md
            names in its rubric. Rejected conforming only new routes: that buys a permanent
            exception to a standard claiming universality, in exchange for avoiding a rename no
            consumer exists to notice. Scope, stated because it is easy to over-read: this binds
            the CONTROL PLANE, which is Bakobo's own invention. The keripy-facing witness interface
            — receipting, OOBI, whatever keripy defines a witness to serve — is not Bakobo's to
            restyle and stays exactly as keripy specifies it, per @w7c4mz. Accepted tradeoff: the
            standards take the /v<major>/<component>/ path segment from the glossary, and `witness`
            is not a lemma yet, so the error envelope can land now while the path rename waits on
            minting it.

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

    The deployable artifact is a container image built from this repo = decision:
      id: lypmcw7f
      why: >
        This repo owns the keripy pin (a direct git reference in pyproject.toml), so building the
        image anywhere else lets the image and the pin drift — precisely the class of problem
        containerizing is meant to close. Rejected building in bakobo/infra: infra is the estate and
        this repo is the software, and the drift risk falls on whichever side does not hold the pin.
        infra consumes an immutable digest, never a tag. Accepted tradeoff: this repo takes on a
        publish pipeline and a retention policy it did not previously need, and — see @a24p3kbw —
        containerizing introduces an LMDB concurrency hazard that the present two-systemd-unit
        deployment does not have.
      children:

        Debian slim base with exactly one native dependency = decision:
          id: vqdcca23
          why: >
            python:3.14-slim-bookworm plus libsodium23 is the whole native surface: pysodium is a
            ctypes binding that dlopen()s libsodium.so.23 at import, and without it `import keri`
            raises at keri/help/helping.py:14. Measured 2026-09-03: no compiler, no headers, no
            build-essential — every C extension in the closure (lmdb, cryptography, blake3, msgpack,
            cffi, multidict, rpds-py, cbor2, PyYAML) ships a cp314 or abi3 manylinux wheel, and the
            three sdists uv builds (pysodium, hio, keri) are pure Python. 241 MB uncompressed, 53 MB
            compressed; 20.6 s cold. Rejected Alpine, and rejected imitating the working Alpine
            Dockerfile in the private, other-org provenant/witness-qualifier: musl has no manylinux
            wheels, so Alpine turns every C extension into a source build and forces
            gcc/musl-dev/linux-headers into the image. That friction is Alpine, not keripy — if you
            find yourself installing a compiler you have chosen the wrong base. Bookworm also
            matches the production host OS; the container's job is to supply the 3.14 interpreter
            that Debian 12's system Python (3.11) cannot.

        Publish to GHCR private  tagged by commit and release  consumed by digest = decision:
          id: lnk24kwp
          why: >
            GHCR private, chosen over ECR because infra bought a vendor-neutral configuration layer
            whose Ansible contract is "a Debian-family host reachable over SSH", and `aws ecr
            get-login-password` would put an AWS-specific step in it. ECR's real advantage is a host
            authenticating with its own identity and storing no secret, but Lightsail has no
            instance roles, so that advantage does not exist here and both registries need a
            short-lived token injected at deploy time. Name: ghcr.io/bakobo/witness. Tags:
            sha-<full-commit> on every build, plus v<major>.<minor>.<patch> on a release tag.
            Deliberately NO `latest`: a floating tag is exactly the image-to-pin drift this node
            exists to close, and its absence makes the mistake unavailable rather than discouraged.
            The commit tag is the audit trail — it maps an image back to the tree holding the keripy
            pin — and the semver tag is the human handle for "the version we shipped"; infra
            resolves either to a digest and deploys that. Rejected commit-SHA tags alone: nothing
            outside the digest would then name a release, so every conversation about what is
            deployed has to go through a lookup. Every workflow action is pinned by commit SHA. A
            retention policy is set from the start: the realistic risk at 53 MB compressed, with
            layers shared across versions, is untended accumulation rather than per-pull cost.
            Accepted tradeoff: a GHCR token must reach the deploy path.
