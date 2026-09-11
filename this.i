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
      children:

        Escrow processing is paced at 1 Hz by substituting one doer at launch = decision:
          id: zj3h2pzh
          why: >
            Measured: a witness sustains at least 131 legitimate events per second with ZERO loop
            lag, while five unanswerable queries per second cost it 0.077 s of lag. It is not slow;
            it has one pathology. Legitimate ingest is O(1) per event, but escrow processing is
            O(escrow depth) per loop PASS, and hio runs that pass 32 times a second — so at a depth
            of 300 the witness performs ~9,600 escrow-entry visits per second against 131 real
            events. The escrow outweighs the entire legitimate load by roughly seventy to one, and
            @znm5uppx addressed only one of the two terms.
            hio decides the cadence from what a doer YIELDS: Doist.recur reads a falsy yield as
            "rerun next pass". keripy's WitnessStart.escrowDo ends its loop with a bare `yield`, so
            it runs every pass. Note that setting the doer's `.tock` does NOT change this, which is
            the obvious thing to try — escrowDo yields its tock only on its first yield and bare
            yields forever after, so the attribute delays the first run and nothing else. Verified.
            So the launcher REPLACES that one entry in WitnessStart.doers with a generator that
            calls the same four escrow methods and yields an interval. Measured: 32 passes per
            second becomes 1. Nothing in keripy is edited, no class is patched, and no module is
            monkeypatched — DoDoer.doers is a settable property read at enter time, and the object
            was handed to us by setupWitness. Combined with @znm5uppx the escrow term falls by
            about 160x from stock.
            Be honest about what this is, because it is a step past @n5r2vq's "add doers": it
            SUBSTITUTES one, so this repo now decides when keripy's escrows run. The reproduction
            is six lines and is guarded by a contract test that parses keripy's own escrowDo and
            fails if the set of calls it makes ever changes — because a fifth escrow added upstream
            would otherwise be silently dropped, and a witness that stops draining an escrow is a
            witness that stops receipting some class of event. Rejected setting a class attribute
            (there is none), rejected forking keripy (@w7c4mz), and rejected leaving it upstream-only
            (the fix is wanted now and the change is ours to make safely). Accepted tradeoff: an
            escrowed event resolves up to one interval later — a second, against a 60-second
            escrow lifetime — and `--escrow-interval 0` restores stock behaviour exactly.

        The query-not-found escrow is held for 60 seconds  not 300 = decision:
          id: znm5uppx
          why: >
            Measured (docs/escrow-load.md, ~4ekl): keripy re-walks the whole query-not-found
            escrow every hio pass, the cost is linear at ~0.35 ms per entry, and sustained depth is
            the attacker's request rate times the timeout. So a witness is measurably degraded at
            UNDER ONE unauthenticated request per second — 200 entries — which is one to two orders
            of magnitude below what the depth figure suggests, and far below any rate limit loose
            enough for a witness that serves KELs to strangers. The timeout is the one term in that
            product we control, so 300 becomes 60 and the sustained cost of a given attack rate
            falls fivefold. Configurable, because the trade is real and belongs to whoever operates
            the witness: the escrow exists so a query arriving just BEFORE the KEL it asks about
            can still be answered, and shortening it drops such a query sooner, leaving the querier
            to retry. Rejected filtering queries for AIDs this witness does not hold, which sounds
            obvious and is wrong — a witness learns its own controller set FROM their inceptions,
            so before one arrives it cannot know, which is precisely why the escrow exists.
            Rejected reaping the escrow ourselves: in-loop it is exactly the computing @e3uji3mv
            forbids, and from the control plane it needs the write access @k3p7wr denies; changing
            a supported knob beats weakening either. Set as a keripy class attribute at startup,
            which is keripy's own configuration idiom (Baser.MapSize is the same shape), so
            @w7c4mz's unforked dependency stays unforked. Accepted tradeoff: a genuinely early
            query now has 60 seconds rather than 300, and the depth is still unbounded — capping it
            outright is an upstream change, and the measurement is what would make that case.

        In-loop telemetry publishes to a segment the control plane maps read-only = decision:
          id: vxt7feoi
          why: >
            Two signals are worth having and neither is reachable from outside the process: loop
            lag, which infra named its highest-value alert because it measures the harm directly,
            and per-doer timings, which is ~2x3n — witnesses have wedged in production, black-box
            probing says only THAT the loop stalled, and naming the doer is what a fix or an
            upstream report needs. Rejected hio's Boss/Crew multidoing first, per reuse-before-build
            and ~5gsx: Bosser spawns Crewer CHILD PROCESSES that talk UXD memos, which is the right
            shape for sending commands INTO the loop and the wrong one here — it would make the
            control plane a child of the witness, require it to run a Doist instead of waitress, and
            put socket servicing on the loop every pass. Rejected writing telemetry into keripy's
            own LMDB: that adds write churn to the witness's store at loop frequency, against a
            100 MB map ceiling. Chose a fixed-size mmap segment with a seqlock: the writer stores
            into already-mapped memory with no syscall and no allocation of consequence, and a torn
            read is detected rather than believed. The pleasing part is that the kernel-enforced
            read-only mapping @a24p3kbw could NOT have for LMDB — because LMDB needs a writable
            lock file — is available here, since a purpose-built telemetry segment has no lock
            protocol at all. Accepted tradeoff: a second, bespoke IPC surface to version, and the
            witness process now contains Bakobo code on its hot path.
          children:

            In-loop code publishes and never computes  and a test freezes that = constraint:
              id: e3uji3mv
              why: >
                The whole argument for the separate-process control plane was that it cannot stall
                the witness BY CONSTRUCTION (@c7v3kp, @k3p7wr). A doer on the Doist gives that up
                and replaces it with a matter of care, which is the position this repo spent a year
                avoiding. The containment is that the in-loop surface stays frozen at publishing:
                no I/O, no syscalls, no LMDB, no imports at call time, no unbounded iteration, and
                no branching on anything a remote party controls. Enforced by a test that parses
                the in-loop module's AST and fails on any call outside an allowlist, because the
                failure mode is not one bad commit — it is "just one more small doer", repeated,
                until the process is in-process by accretion without anyone deciding to. A note in
                a docstring would not have stopped that; a red test does. Accepted tradeoff: the
                allowlist has to be widened deliberately, in a commit that says why, which is
                exactly the friction being bought.

            Per-doer timings wrap deeds rather than patch hio = decision:
              id: vpu373to
              why: >
                Doist.enter() returns the deeds deque of (dog, retyme, doer) triples, so overriding
                only enter() and replacing each dog with a transparent timing generator leaves every
                doer object untouched — identity, .tock, .done, .opts, .temp — and reproduces none
                of upstream's scheduling logic, so the drift surface against a keripy or hio bump is
                three lines. Rejected subclassing Doist.recur (copies ~40 lines of upstream that
                will drift) and rejected wrapping the doer OBJECTS (hio both reads and writes
                doer.done, including via doer.__func__ for bound methods, so a proxy has to be
                perfect in two directions). Verified transparent on Python 3.14: send() passes the
                yielded tock through, close() returns the inner generator's value — which is what
                Doist.exit assigns to doer.done — and StopIteration.value propagates. The forensic
                payload is a "currently executing doer" slot stored BEFORE each send: when the loop
                wedges, that field already names the culprit, which a duration-only scheme records
                too late to be useful. Accepted tradeoff: one extra generator frame per doer per
                pass, and a wrapper whose correctness the witness now depends on.

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

            Dissolved 2026-09-09 by @s6v3qm's amendment. heti is no longer a dependency, and fiki —
            which replaces it — has no keripy dependency to collide with ours, by its own
            construction. keri now resolves once because only witness asks for it, so the constraint
            this node existed to hold has no second party. The pin itself stands — this repo owns it
            (@lypmcw7f) and the drift workflow still watches it; what goes away is the co-resolution
            hazard and the gated-bump ceremony around it. Kept rather than deleted because this node
            is why the pin is where it is, and a reader finding 366d8107 deserves the history.

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

        The P2 read surface is nine GET nouns and their response shapes = decision:
          id: zc7p7qth
          why: >
            @h5n2rk made an endpoint's response shape a node-governed external contract, and the
            P2 surface shipped seven new ones without a node — @zzbdxa governs the GRAMMAR those
            paths follow, not which paths exist. This node is the missing record, and it enumerates
            them so a later change has something to be a change TO: health (status, and ticks when
            telemetry is available), identity (aid, alias), version (witness, keripy), loop (the
            telemetry segment), escrow (depth per store, total), database (path, used_bytes,
            map_bytes, used_fraction, last_transaction, readers), process (the runner's vitals),
            controller (aid, sequence_number, said per held controller), and controller/{aid}
            (adding witnesses and threshold). The carve follows url-design's insight test: /info
            was split because varying the keripy version, the database path and the AID each change
            the shape of a different answer, so grouping them under one noun predicted nothing.
            Health gained a `ticks` field and a `degraded` status served at 200, which @h5n2rk's
            freeze did not contemplate: ops.md §7 asks a probe to prove the service is working, and
            a witness whose loop has wedged opens its database perfectly. Degraded is 200 rather
            than 503 deliberately — the control plane IS working and is reporting accurately on
            something else, and an HTTP status is a verdict on the exchange rather than on the
            subject (http-errors.md). Accepted tradeoff: nine shapes are now frozen contracts, and
            the read surface is unauthenticated until @s6v3qm lands, which is why the port binds
            behind the estate's proxy.

        Metrics leave over OTLP  and only when an endpoint is configured = decision:
          id: wea6qjmk
          why: >
            ops.md §7 is explicit — OpenTelemetry SDK inside code Bakobo writes, a Collector on
            every host, OTLP on the wire, a hosted backend — and infra asked for these signals in
            those terms. So the control plane exposes escrow depth, loop lag, database usage and
            process vitals as OTel observable gauges rather than as a scrape endpoint. Rejected
            hand-rolled Prometheus text, which is what the Provenant witness does and what an
            earlier draft of this repo's agenda copied from it: it is cheaper and it is against the
            standard, and adopting another org's answer because it was nearby is how a convention
            gets laundered. §7's own split also lands the right way round here — the control plane
            is code Bakobo writes and gets application metrics; the keripy witness is a third-party
            process we merely run, so what we publish about IT is host metrics and black-box
            probes, which is exactly what these gauges are. Observable (callback) instruments, not
            counters we increment, because the values already live in LMDB and the telemetry
            segment: reading them at export time is one pass rather than a shadow copy that can
            disagree with the source. Export is off unless an OTLP endpoint is configured, so the
            image runs with no collector and the witness never blocks on one; a callback that
            raises is dropped rather than propagated, because a witness that is down is the very
            thing the metrics are meant to report. Accepted tradeoff: two more packages in the
            certified closure of a security-sensitive image, justified by their living only in the
            control-plane process, which @c7v3kp already says cannot affect the witness.

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

    Authenticate with RFC 9421 message signatures via fiki = decision:
      id: s6v3qm
      why: >
        The control plane authenticates each request with an RFC 9421 HTTP Message Signature: the
        caller proves a non-transferable AID, which is stronger than a shared bearer secret and fits
        the KERI posture. Reuses a library's 9421 verifier rather than reimplementing 9421 or using
        bearer tokens (reuse-before-build). Authorization is an operator-AID allowlist. Accepted
        tradeoff: callers must sign requests, a higher bar than presenting a token.

        Amended 2026-09-09: the verifier is fiki, not heti. This node originally named
        heti.l0.verify_request, then heti.ephemeral.verify_request after a rename. That bundle has
        since been extracted into bakobo/fiki as a library of its own (fiki this.i @07wstqk7), so
        depending on heti for it now buys the whole KERI stack to get a function that deliberately
        does not use it — fiki forbids keripy and bakobo-errors as dependencies rather than merely
        omitting them, and enforces it with a test. Two consequences follow and are accepted. First,
        fiki is public, so witness's only private dependency is gone and a clone resolves
        anonymously — which is what lets this repo be public at all (@qojsxe7s). Second, fiki raises
        typed exceptions under FikiError and carries no Bakobo error codes, so the mapping onto
        e.input.*/e.proof.* that heti did at its boundary does not come along: witness must do that
        translation itself when auth lands, against the error-codes standard. Rejected keeping heti
        for the code mapping, which would reintroduce the private dependency to avoid writing a
        dozen lines of translation. Also noted: fiki's verify_request takes url rather than path and
        requires an explicit max_age, so the call site must decide a replay window rather than
        inherit one.

    Implement in Python to reuse keripy = decision:
      id: d4h7kt
      why: >
        The control plane reuses keripy's own DB accessors and CESR/Serder to read and render
        witnessed state; a different language would reimplement keripy's LMDB schema and CESR
        parsing. Driving constraint: Python >=3.14, which is keripy's floor. Amended 2026-09-09:
        this node also cited heti, which is no longer a dependency (@s6v3qm). Its replacement, fiki,
        floors at 3.11 and so constrains nothing here — keripy alone sets the floor now.

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
      children:

        The CLI is three subcommands  the third being the in-image supervisor = decision:
          id: ixdaut53
          why: >
            @g3w6px named two process roles, `control-plane` and a later `run`. Both now exist, and
            a third shipped without being named: `supervise`, which @a24p3kbw's one-container shape
            requires — something has to start both processes, forward the container's stop signal,
            and enforce the asymmetric failure policy @c7v3kp implies. It belongs on this CLI by
            @g3w6px's own argument (one entry point operators install and discover) even though it
            is not a witness role in the way the other two are: it is the thing that runs roles.
            Rejected a separate `witness-supervise` console script, which fragments discovery
            exactly as @g3w6px rejected; rejected supervisord and s6, because the org standard is
            provable 100% branch coverage and neither an untested shell entrypoint nor an opaque
            third-party binary can meet it, and neither gives the asymmetric policy by default.
            Accepted tradeoff: this repo now owns process-supervision code, a category of bug it
            did not previously have.

        Intent for @wea6qjmk landed in the same commit as its code = deviation:
          id: 2gep42zc
          why: >
            methodology §5 requires the intent commit to precede the code commit it justifies, and
            every other node here did that — @lypmcw7f, @a24p3kbw, @vxt7feoi and their children all
            landed code-free. @wea6qjmk did not: commit 7493ade carries the node together with
            metrics.py and its tests. Recorded as a deviation rather than repaired, because the
            repair would be a history rewrite to buy back a discipline whose whole value is that it
            was observable at the time — and it was not. §7 says deviation nodes are the complete
            list of approved gaps, so an unrecorded gap is a defect while a recorded one is a
            judgment. Found by the v0.1.0-rc review panel (CON-F2) rather than by the author, which
            is what running one is for.

    A backup is a consistent hot copy of every store  taken read-only = decision:
      id: 7b34ohbo
      why: >
        @a24p3kbw made an upgrade across a keripy migration a one-way door — keripy refuses to open
        a database written by a newer library — so restoring a backup IS the rollback path, and a
        rollback plan that has never been exercised is a hope. `witness backup` uses LMDB's own
        `env.copy`, which takes a transactionally consistent snapshot from a READ-ONLY environment
        while the witness keeps running. That is strictly better than the tar-the-volume advice it
        replaces on both counts an operator cares about: no downtime, and consistent rather than
        merely crash-recoverable. Read-only because @k3p7wr's guarantee is not suspended just
        because the operation is administrative.
        The part that is easy to get wrong and fatal: a witness is FOUR stores, and the signing
        keys are not in the one anybody thinks of. Backing up `db` alone restores a witness that
        holds every event and cannot sign a single receipt — dead while looking alive — so backup
        covers the keystore, the event database, the credential registry and the config together,
        and the restore test proves the restored witness still RECEIPTS rather than merely still
        answering. Rejected a control-plane endpoint: an HTTP surface that writes files wants the
        authentication @s6v3qm has not landed yet, and an operator with a shell already has one.
        Rejected a `witness restore` subcommand: restore writes into the witness's own volume,
        which is the one direction this repo has spent its whole design avoiding, and it is a
        directory copy an operator can audit. What needed proving was that a restored volume
        WORKS, and that is a test rather than a command. Accepted tradeoff: an operator composes
        the restore from documented steps, and a backup taken with the witness running captures
        the moment `env.copy` began rather than the moment it finished.

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

        Publish to GHCR  tagged by commit and release  consumed by digest = decision:
          id: lnk24kwp
          why: >
            Corrected 2026-09-10: this node said "GHCR private" and that is no longer true. The
            package ghcr.io/bakobo/witness is public and an anonymous client resolves its tag list
            and manifests with no credential — verified against the registry rather than inferred
            from a setting. I cannot date the flip from the API: a package's updated_at tracks its
            newest version, not its visibility, so all that is established is the current state.
            The likeliest cause is @qojsxe7s making the repo public, since GHCR ties a package's
            visibility to the repository it is linked to. Everything below about naming, tagging
            and digest consumption is unaffected; only the audience changed, and the change is the
            right one for an Apache-2.0 repo whose whole point is that other operators can run it.
            The last sentence's "a GHCR token must reach the deploy path" now holds for PUSH only.

            GHCR, chosen over ECR because infra bought a vendor-neutral configuration layer
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

    Throwaway witness pools are a verb of this repo's own CLI = decision:
      id: n2bgpdds
      why: >
        Experimenting against a set of witnesses is a standing need — merti's proof-of-email
        ceremony needs witnessed AIDs whose OOBIs resolve, and every question about toad, receipt
        thresholds or a witness going away needs more than one witness to ask. `witness pool up`
        stands up N containers of THIS repo's image, each with its own volume, keystore, PID
        namespace and published ports, and `witness pool down` removes every container and volume
        the pool created. Rejected in-process witnesses under one hio scheduler, which is what
        bakobo/heti already has (`heti/tests/produce/witnesses.py`, and `heti demo
        --keep-witnesses`): they are genuine keripy witnesses and they are the right tool for a
        question about the KERI protocol, but they are not this repo's artifact. What a pool is for
        here is the image — the supervisor's failure policy, the HEALTHCHECK, the control plane
        answering while the experiment runs — and @a24p3kbw makes the per-witness PID namespace
        load-bearing rather than decorative, so a testbed that shares one models nothing that
        matters. Rejected a compose file, generated or hand-written: it would be a second statement
        of what is running, and it is the wrong thing to key cleanup on. Every container and volume
        carries a `bakobo.pool=<name>` label instead, so an `up` interrupted halfway is still
        removable in full by a `down` that knows nothing about how far it got. Rejected `compose up
        --scale`, which would need the image to initialise its own keystore — the auto-init that
        tick `5dnx` blocks until a witness refuses to start over a database whose keystore has gone
        missing. Rejected a loose script outside the package: @g3w6px already says one CLI with a
        subcommand per role, and the coverage gate measures the `witness` package only, so a script
        beside it would ship untested by construction. Accepted tradeoff: a laboratory verb rides
        inside the production image, where `docker` is absent and it fails with a typed error; and
        the pool holds the default port range 5640+ per witness, which is an assumption about the
        host rather than a fact about it.

        Two mechanisms are worth naming because the obvious versions of both are wrong. The
        witness's own advertised URL (its `curls`, which is how a resolver learns where to reach it
        after an OOBI) is seeded by writing keripy's config file into the volume at keripy's OWN
        default location, `/usr/local/var/keri/cf/<alias>.json`, rather than by passing
        `--config-dir` in an overridden container command. An override would mean restating the
        image's CMD in the pool's own code, where it would drift the first time the image's command
        changes and would do so silently. And the pool keeps no state file: the manifest is derived
        live from the docker labels and each control plane's `/v1/witness/identity`, so what the
        pool reports cannot disagree with what is running, and `down` leaves nothing behind on disk
        to clean up separately.
      children:

        A pool serves witnesses and describes them  it never incepts = decision:
          id: jorbhpfq
          why: >
            The pool creates no controller and holds no controller's keys. What an experiment
            actually needs is a witnessed AID, and inception belongs to whatever holds the keys —
            heti's lockbox, or `kli incept` — so the pool's deliverable is the description those
            tools take as input: heti's standing witness set (`[witnesses] oobis / toad`, the exact
            block `heti/src/heti/tui/config.py` reads) and a `kli incept --file` document carrying
            `wits` and `toad`. Rejected a `pool incept` verb, which was in an early draft of this
            design: it would put key material in a witness-side laboratory tool, duplicate what
            heti exists to do, and buy nothing that piping a manifest does not. The suggested toad
            is keripy's own `ample(n)` rather than a number of ours, so the default threshold is
            the protocol's opinion and not this repo's. Accepted tradeoff: standing up a pool and
            getting a witnessed AID are two commands from two repos rather than one.

    The repository is public under Apache-2.0 = decision:
      id: qojsxe7s
      why: >
        witness is published: the GitHub repository is public and the tree carries the Apache-2.0
        text. Recorded 2026-09-09. The driver is that every KERI operator running a stock keripy
        witness has the observability problem this repo solves, and @q4m7tz already committed to
        solving it WITHOUT forking keripy — which is the property that makes the answer portable to
        operators who are not Bakobo. Publishing is also what lets this repo be offered to the KERI
        Foundation, which is the occasion for the decision rather than its justification.

        Two things had to be true first and now are. The dependency closure resolves anonymously:
        heti was the one private dependency and @s6v3qm replaced it with fiki, which is public, so a
        stranger's `uv sync` succeeds where before it needed an SSH key against a repo they cannot
        see. And the credential machinery that existed only to reach heti from CI and from the image
        build — the bakobo-dependency-reader App token, the insteadOf rewrite, the
        persist-credentials:false that the rewrite made load-bearing, the gh_token build secret — is
        removed rather than left inert, because machinery nobody needs is machinery nobody maintains.

        Accepted tradeoffs. The unauthenticated read-only control plane and the escrow-amplification
        note in docs/deploying.md are now public reading, so the deployment contract has to hold on
        its own merits rather than on nobody looking; @h5n2rk and @t3k6ps already took that risk
        deliberately for a read-only surface carrying no key material, and publication does not
        change the analysis, only the audience.

        Corrected 2026-09-10, the day after. This paragraph ended by naming a gap — that the GHCR
        image stayed private, so a reader could build the image but not pull the one Bakobo ships,
        "a gap worth closing deliberately rather than by drift, and not closed here." The gap does
        not exist: the package is public and pulls anonymously. The claim was taken from
        @lnk24kwp's text rather than from the registry, which is the whole lesson — a node was
        treated as evidence of the world's state when it was only evidence of a past intention, and
        the sentence warning against drift was itself the drifted one. Check the artifact, not the
        record of the artifact. @lnk24kwp is corrected too.

    Changes reach main through a pull request  admins may bypass = decision:
      id: lu4a5qy6
      why: >
        Recorded 2026-09-10. `main` carries an active ruleset requiring a pull request and the
        `test` and `image` checks, with `deletion` and `non_fast_forward` also blocked. Work
        branches, opens a PR, and merges once CI reports. No approving review is required, because
        requiring one on a repo with a single maintainer would only produce a habit of
        self-dismissal; the value bought here is that every change to a public repo passes CI
        before it lands, not that a second human reads it. Merge or rebase, never squash.

        Organization admins keep an always-bypass, deliberately. A ruleset with no escape hatch
        fails hardest exactly when the process itself is what is broken — a green build blocked on
        a check that will never report, a CI outage during an incident. What makes the bypass
        acceptable is that it stays rare and deliberate, and what threatens that is a property
        worth naming: **the bypass is silent from the pushing side.** Nothing prompts, nothing
        confirms; `git push` succeeds and the remote mentions `Bypassed rule violations` in output
        a person may not read. So the guard is written into AGENTS.md as a rule an agent reads
        before working here, rather than left to the ruleset to enforce, because the ruleset by
        construction will not enforce it against the actor most likely to trip it.

        The occasion: the repo went public (@qojsxe7s) and the ruleset had been in place but
        unhonored — two pushes to `main` bypassed it in as many days, both by an admin who did not
        set out to bypass anything. The ruleset did not change; the practice did. Accepted
        tradeoff: a one-line fix now costs a branch, a PR and a CI round-trip, which is the price
        of the checks actually gating anything.

        A consequence for tooling: `git-autopush` pushes the CURRENT branch when it fast-forwards,
        so it will push a feature branch (wanted, it feeds the PR) and will push `main` if a commit
        is ever left sitting there (not wanted, and it would bypass). Keeping work off `main`
        locally is what makes the nightly job safe here, rather than any setting in the job.
