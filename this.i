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

    Input completeness is enforced by a door census  with a review date = decision:
      id: bb3yndtf
      why: >
        dev/standards/input-handling.md asks every repo that handles input to carry ONE test
        enumerating the primitives that bring bytes across a boundary and assert each call site
        sits inside a named door or is exempted with a written reason. This repo had doors and no
        census, so nothing established the set was complete — which the standard names as the part
        that decays, because a new call site that reads a file directly does not look wrong when
        you write it.
        Built as an AST scan over src/witness rather than a grep or a review checklist, matching
        the pattern Bakobo already runs in the alias quarantine and the untrusted-output filter. It
        fails in BOTH directions: an unlisted crossing fails it, and a listed entry that no longer
        matches any crossing fails it too, so the inventory cannot rot into a description of code
        that has gone. The scan is itself tested against planted source, because a census that
        silently found nothing would pass just as quietly as a correct one.
        A LAST_REVIEWED date sits beside the inventory. The census catches a new call site by
        itself; what it cannot catch is an exemption whose REASON quietly stopped being true, and
        only a person re-reading them catches that. Recorded rather than enforced: the date is
        asserted to parse and to not be in the future, and staleness does not fail the build.
        Rejected failing on age, which would invent a review cadence nobody has chosen and would
        be satisfied by bumping a constant rather than by reading anything.
        Rejected a minimum length on exemption reasons, which a first draft had. The shortest
        reason here — that a call writes rather than reads — tripped it while being complete, and
        a length bar cannot distinguish a padded sentence from a substantial one, so it teaches
        padding. Accepted tradeoff: the census proves accounting, not correctness. An exemption can
        be wrong, and only the review date says when anyone last looked.

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

        A fork-only pin is legitimate only while an upstream PR tracks it = constraint:
          id: enyp5khx
          why: >
            @w7c4mz forbids diverging code, and on 2026-09-16 the pin moved to 173678c0 — a commit
            on bakobo/keripy's feature/witness-decls branch, which upstream has not taken. That is
            divergence unless it is temporary, and the only thing that makes it temporary is a live
            upstream PR somebody is shepherding. So the rule is not "never pin to the fork" but
            "a fork-only pin owes an open upstream PR containing the pinned commit", and the drift
            canary enforces it: it fetches WebOfTrust/keripy main, asks whether the pinned commit is
            an ancestor, and when it is not, hunts the open bakobo-headed PR whose head contains it.
            No such PR is a red build, because at that moment nothing but memory is carrying the
            change home. Rejected asserting the PR number in a file, which is a second copy of a
            fact GitHub already holds and would rot the day the PR is reopened under a new number.

            The canary also stopped conflating two questions it had been answering with one exit
            code. "Has upstream moved under our accessors?" and "is our own feature upstream yet?"
            failed identically on 2026-09-21, and the second one will keep being true for as long
            as review takes — weeks in which a weekly red build teaches the reader to skip the mail
            that matters. Tests that can only pass where fork-only code exists now carry the
            `fork_only_keripy` marker; the canary deselects them, so a failure of the rest is
            unambiguously upstream drift. Running ONLY the marked tests against upstream is then
            the inverse canary: they are expected to fail there, and their passing means the
            feature landed and the pin should come home. One marker, both directions, no second
            list of test names to keep in step.

            Accepted tradeoff: a marked test is unexercised against upstream main until it merges,
            so drift in the fork-only code itself surfaces only at re-pin. That is the same bargain
            @w7c4mz already struck for everything else, and the code in question is ours.

        The canary explains itself to someone who has only the email = constraint:
          id: 7enojuyn
          why: >
            An operator who gets "Run failed: keripy drift canary" at 06:00 on a Monday cannot act
            on it. The run page says which assertion failed, not what the failure MEANS, and the
            meaning here is genuinely non-obvious — nothing on main is broken, no deploy is at
            risk, and the correct response is usually to do nothing yet. Reconstructing that from
            a stack trace is a research task, and one this repo has now paid for twice.

            So every outcome the canary can reach writes a paragraph naming the situation, what is
            and is not at risk, what would resolve it, and where the governing decision lives — and
            that paragraph goes into a GitHub issue, because an issue body is the only channel here
            whose full text reaches a mailbox. Workflow annotations and job summaries are one click
            away from the failure mail and are used too, but they are not the artifact; the issue
            is, and it stays open for exactly as long as the condition holds. Rejected commenting
            on each weekly run (52 mails saying the same thing) and rejected composing the mail
            ourselves through an SMTP action (a credential to hold, and a second delivery path to
            keep working). Accepted tradeoff: the job needs `issues: write`, and a reader who
            watches the repo gets an issue they did not open.

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

        A witness broadcasts tags about itself  and testnet is the first one = decision:
          id: vqqh6zdk
          why: >
            A witness needs a way to say "I am laboratory infrastructure, do not build anything
            consequential on me," and an AID witnessed by such a witness inherits that. Rejected
            carrying it as a keripy configuration trait in the witness's inception event, on two
            measurements against the pinned keripy: a witness AID is non-transferable by necessity
            (@c7v3kp's sibling reasoning in reader.py — a transferable witness would need witnesses
            of its own and the definition would not terminate), so its `B` prefix IS the public key
            and does not commit to the event's contents; incepting one key with and without
            cnfg=["NP"] yields the identical prefix and only a different event SAID. And the icp of
            a non-transferable AID has no audience, because verifying a witness receipt needs the
            prefix alone, so nothing in the ecosystem fetches that KEL. Two further facts make the
            trait carrier worse than it looks: TraitCodex membership is unenforced on the JSON path
            but enforced on the CESR-native one (Traitor raises InvalidSoftError, coring.py), so a
            private code is mintable today and unmintable by anything CESR-native; and Kever.state()
            rebuilds the trait list from two hardcoded booleans, so an unrecognized trait never
            reaches key state at all. Also rejected an arbitrary anchor in the icp `a` field, which
            Serder validation refused on both serializations on first inspection. Chosen instead:
            signed `rpy` messages the witness issues about itself — the same channel /loc/scheme and
            /end/role already use, and which exists precisely because a non-transferable AID's
            degenerate KEL cannot carry facts about the witness. Accepted tradeoff: tags are mutable
            at the source rather than frozen into the identifier, so "cleared by nothing" must be
            enforced at the consumer (@dhh2gnvv), and increment 1 ships them as operator config with
            no cryptographic binding at all until the `rpy` route lands.
          children:

            Tags are names  not key/value pairs = decision:
              id: k3tkkss2
              why: >
                A tag is a bare name and the collection is a list. The consumer question is always
                membership — "does this witness claim X" — and a map invites a schema argument per
                key plus a versioning problem this feature does not have. Rejected a fully open
                namespace, where every deployment invents its own spelling of testnet and nothing
                interoperates: bare lowercase names are a defined vocabulary that a peer may reject
                as a typo, and a dotted vendor prefix (bakobo.pool) is anyone's to mint and must be
                passed through opaquely. Accepted tradeoff: a tag that genuinely needs a value
                arrives later as a separate member rather than by stringly-typing this one.

            The vocabulary asserts only the negative claim = constraint:
              id: pmtzkn6j
              why: >
                There is a `testnet` tag and there is deliberately no `production` tag. A witness
                asserting it is production makes a self-serving claim nothing verifies; a witness
                asserting it is testnet speaks against its own interest, which is the only kind of
                self-assertion worth reading. So the derived value is three-valued — testnet,
                unmarked, and never production-asserted — and absence is never assurance, matching
                COIA APPENDIX-D flag 6's rule that a reader MUST NOT render absence as a positive
                assurance. Rejected a boolean `production` member, which would make every unmarked
                witness in the world look affirmatively production-grade. This narrows org principle
                8 rather than contradicting it: treating unmarked as testnet would taint every AID
                in existence today and make the feature useless, so failing closed here means
                refusing to read absence as a positive signal, not manufacturing a negative one.

            Stickiness lives at the consumer  not at the witness = decision:
              id: dhh2gnvv
              why: >
                COIA flag 6 is cleared by nothing — a test identifier does not become a production
                identifier — but @vqqh6zdk establishes that no carrier available to a witness can
                make its own assertion immutable. So the monotonicity is enforced where the
                judgement lives: once a consumer has seen a witness tagged testnet, that marking
                never clears for that consumer, whatever the witness later says. Rejected letting
                the tag clear at the consumer too, which would let a laboratory witness retroactively
                launder every AID it ever witnessed by re-tagging itself. Accepted tradeoff: two
                consumers can legitimately disagree about the same witness, which is COIA's
                creator-indexed class, and a consumer with no history sees only today's claim. The
                derivation function is therefore pure and the caller owns persistence.

            Attributes are a separate map that no AID inherits = decision:
              id: e4ceoopg
              why: >
                @k3tkkss2 said a tag needing a value would arrive as a separate member rather than
                by stringly-typing the name, and this is that member: `attributes`, a map of key to
                value, alongside `tags`. The line between them is not mutability but whether a
                consumer DECIDES on the value or merely DISPLAYS it. A consumer acts on `testnet`,
                so it must be a predicate with agreed semantics; a consumer shows a human
                `operator` or `contact`, and should act on neither.
                The decisive reason they cannot be one thing is that union is defined for names and
                undefined for pairs. Tag inheritance is a union over an AID's witness set — any
                witness tagged testnet taints the AID, monotonically and obviously. Three witnesses
                reporting region=eu, region=us and region=ap have no natural merge, and every
                artificial one is a rule somebody must agree to. So attributes are deliberately NOT
                inherited: controller/{aid} carries derived tags and no attribs member at all, and a
                test asserts that absence so the omission stays a decision rather than decaying
                into an oversight. The second reason is evidential — @pmtzkn6j accepts `testnet`
                because it is a claim against the witness's own interest, and no valued attribute
                has that property, so a self-reported region is worth exactly what a self-reported
                `production` would have been.
                Rejected folding both into one noun. The signed reply routes of @vqqh6zdk's second
                increment want to be separate, because BADA orders each route independently and
                tags change almost never while a contact address changes often: one route would
                mean re-signing and re-timestamping the testnet assertion to correct a typo in an
                email address. Two nouns keep the local surface isomorphic to the protocol surface.
                Accepted tradeoff: a consumer that wants both makes two requests.
                Keys follow the same rule as tag names — bare keys are a defined vocabulary
                (operator, contact, pool), a dotted vendor prefix is anyone's to mint — so a key is
                exactly as interoperable as a tag name is. Values are printable ASCII, non-empty,
                at most 256 characters. Deliberately not Unicode: this is a field a human reads off
                a screen, which makes it a homograph and bidi spoofing surface, and a field nobody
                is permitted to decide on loses little by being restricted. Rejected allowing empty
                values, since a key with no value says less than the key's absence does.

            A seed file in the volume carries declarations a pooled witness cannot get on argv = decision:
              id: hjz7b7qo
              why: >
                A pool is laboratory infrastructure by definition, so its witnesses should say so —
                and until now they could not. @nlunqygr put --tag and --attrib on the control
                plane's command line, and @n2bgpdds deliberately does NOT override the image's CMD,
                because restating the image's own command inside pool.py would drift silently the
                first time that command changed. So argv is closed to the pool by a decision worth
                keeping, and the declarations had to arrive some other way.
                Chosen: a JSON file the pool writes into the volume at KERI_HOME/decls.json, read by
                the control plane from that path by default, so the image's CMD stays untouched and
                a pooled witness declares itself with no new argument anywhere. It rides the
                one-shot seeding container the pool already runs for the advertised URL rather than
                adding a second, so `pool up` costs no extra container per witness. Rejected a
                WITNESS_TAGS environment variable: it also leaves CMD alone, but tags and attribs
                would need either two variables or a packed encoding invented here, where one file
                carries both in a shape that already exists. Rejected folding declarations into
                keripy's own cf/<name>.json, which is keripy's file and whose keys are keripy's to
                define; when the /decl routes land upstream that file becomes the right home, and
                this one goes away.
                Precedence per endpoint, highest first: a signed declaration, then --tag/--attrib,
                then the seed file. A flag beats the file because whoever typed it is acting now,
                where the file was written when the volume was provisioned; that is the ordinary
                command-line-over-config rule and inverting it would make a flag silently
                ineffective. `source` therefore has a third value, seed-file, rather than reporting
                the file as operator-config — an operator debugging a witness that claims something
                unexpected needs to know which of three places to go and look.
                Accepted tradeoff: this is a second input door for the same values, and a file in a
                volume is easier to forget than an argument in a command. The door is bounded like
                the others (size, then shape, then meaning) and a missing or unreadable file is a
                witness with no declarations rather than a witness that fails to start.

            Testnet taints a whole delegation chain  as far as we can follow it = decision:
              id: 4qrayq3j
              why: >
                A delegated AID's authority is rooted in its delegator: the delegating event has to
                be anchored in the delegator's KEL, so anyone who accepts the delegate has already
                accepted the delegator. If laboratory witnesses receipted the events that brought a
                delegate into existence, the delegate rests on infrastructure nobody promised to
                keep, and the taint carries. So derivation unions the witness sets of every level
                of the chain, not just the AID's own.
                Rejected stopping at the AID's own witness set, which was what shipped in
                @vqqh6zdk's first increment and was recorded as undecided rather than decided. The
                argument for stopping is that a delegate's ONGOING key state is established by its
                own witnesses and the delegator's mattered only at the moment of delegation. That is
                true and does not rescue it: the question testnet answers is whether anything
                consequential should rest here, and a history that only laboratory witnesses ever
                receipted is not a history to build on, whoever is receipting today.
                The chain is followed only as far as the local database reaches, and that is
                expected rather than a defect. A witness is under no obligation to hold key state
                for a delegator, so the walk commonly stops early. A delegator we cannot follow is
                reported in an `unfollowed` member, separate from the `unresolved` witnesses,
                because the two call for different follow-up: a witness we cannot speak for is
                somebody to go and ask, where a delegator we cannot follow means there may be
                witness sets we never saw at all. Accepted tradeoff: an incomplete answer is the
                normal answer, and a consumer wanting certainty has to resolve the chain itself.
                Rejected inventing a resolution path for it — @k3p7wr forbids the control plane
                making outbound calls, and that holds here exactly as it does for co-witnesses.

            The read surface gains a tenth noun and controller/{aid} gains a tags member = decision:
              id: nlunqygr
              why: >
                @zc7p7qth froze nine nouns and their shapes expressly so a later change would have
                something to be a change TO; this is that change. Tenth noun: GET /v1/witness/tags,
                answering {tags, source}. And controller/{aid} gains an additive `tags` member
                carrying {derived, from, unresolved}. The `source` member exists so that increment 2
                can move the origin of tags from operator config to a signed `rpy` without changing
                the endpoint contract — it reads "operator-config" now and "signed-reply" then.
                Rejected having the control plane fetch peer witnesses' tags over HTTP to fill in
                `unresolved`: it is a read-only observer (@k3p7wr), and an outbound fetch would add
                an SSRF surface and a network dependency to the one process whose whole charter is
                not having one. Accepted tradeoff: the controller answer is partial by construction,
                so `unresolved` is a first-class part of the response rather than an omission, and
                a caller that wants the whole picture must resolve the other witnesses itself. The
                repeatable --tag flag is an external contract under the methodology §3 trigger and
                is frozen by this node.
                Precedence resolved 2026-09-16, when the read path was built: a signed declaration
                WINS over operator configuration, rather than configuration overriding it. A third
                party can verify the signed one and cannot verify the flag, so letting a local flag
                mask what the witness has published on the wire would make the endpoint disagree
                with the protocol and would hide the disagreement. Rejected the opposite
                precedence, which reads like an operator escape hatch and is really a way to lie
                locally about what you have already said publicly. The endpoint still answers when
                the database cannot be opened, because an operator asks whether this is the
                laboratory box precisely when things are broken; four different absences -- no
                database, a keripy predating the decl routes, an unidentifiable keystore, and
                nothing declared yet -- all fall back rather than fail, since a witness whose
                declarations are still configuration is in a normal state and not a degraded one. It goes on `control-plane` ONLY, and deliberately not on
                `run`: the control plane is the process that serves tags, so a `run --tag` would be
                a flag whose value nothing reads — the same unsupported claim the pyproject note
                refuses to make about fiki. `run` grows one when @vqqh6zdk's second increment makes
                the runner the thing that publishes them.

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
      children:

        A witness refuses to start when its history outlives its keystore = decision:
          id: 3r2xawen
          why: >
            `witness run` compares the two stores before it opens either one, and refuses when the
            database holds this witness's hab and no keystore exists. The combination cannot be
            legitimate — a volume that has never run has neither, one that `kli init` has touched
            has the keystore and no hab yet — so the only way to reach it is for something to have
            removed the keys from under a witness that already exists. @7b34ohbo makes a restore
            the rollback path, and a restore that copies `db` without `ks` is exactly that
            something.
            What made this worth a refusal rather than a warning is that the previous behaviour
            was silent in every channel we have. Measured 2026-09-05 while building infra's
            tier-one restore drill: keripy creates a fresh keystore, and the witness then serves
            the AID recorded in the DATABASE — the one controllers designated and validators
            trust — while signing with keys that AID does not name. It reports `{"status": "ok"}`,
            accepts a controller inception with a 204, and `witness backup` succeeds. There is no
            observation from outside that distinguishes it from a healthy witness, and a validator
            cannot detect it either, because the receipts verify against a key the AID never
            authorised only if you go back to the KEL to check.
            Rejected surfacing it in `/v1/witness/health` instead: the control plane can only
            report it once the witness is already running and already signing, which is after the
            harm. Rejected warning and continuing, for the same reason — the fail-closed principle
            applies hardest where the failure is undetectable downstream. Placed in the runner
            rather than in the image's entrypoint because the comparison needs keripy's own view
            of where the stores are, and because `kli witness start` would have the identical bug.
            The ordering is load-bearing and easy to get wrong: `Keeper(reopen=True)` CREATES the
            keystore it is asked to open, so a probe made after it would find one every time and
            the check would never fire. Accepted tradeoff: a witness whose keystore is genuinely
            gone now crash-loops instead of coming up wrong, which is louder and is the point.
            Opens the door to opt-in first-run initialisation, which this refusal is what makes
            safe — a genuinely fresh volume becomes distinguishable from a mutilated one.

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

        The upgrade oracle takes over a volume the DEPLOYED image wrote = decision:
          id: ktljhcyt
          why: >
            `tests/test_image_upgrade.py` accepts a second image in `WITNESS_BASELINE_IMAGE` and,
            when one is set, drives the handover both ways: the baseline creates the volume and
            gets a controller's inception witnessed, this image takes it over and has to serve the
            same AID, the same key state and accept a FURTHER event — then the reverse, because
            within an unchanged keripy pin a rollback is just redeploying the previous digest and a
            rollback nobody has run is a hope (@7b34ohbo's argument, applied one level up).
            WHAT THIS FIXES IS A CLAIM THAT WAS WEAKER THAN IT READ. The oracle already replaced a
            container over a volume and checked the witness survived, but BOTH containers were the
            same image, so what it proved was container replacement rather than version
            compatibility. Every real upgrade is two versions by definition, and the one property
            an operator is buying — that the new image can pick up where the old one left off — was
            the one not under test. bakobo/infra's `bin/release-witness` had to rest its same-pin
            claim on the two pins being textually identical, which is an argument rather than an
            oracle.
            THE BASELINE IS THE DIGEST PRODUCTION IS RUNNING, not merely the previous release.
            "What we are upgrading FROM" is a fact about the estate, so the value is supplied by
            whoever knows it — infra, at release time, or a repository variable here — rather than
            derived from this repo's own tags, where it would be a guess that looks authoritative.
            ABSENT RATHER THAN DEFAULTED, and the new tests SKIP with a reason naming the variable
            when it is unset. Rejected falling back to this image as its own baseline, which is
            what the oracle already did: the fallback would make a run with no baseline configured
            indistinguishable in its output from a real two-version handover, which is a weaker
            claim wearing a stronger one's clothes. A skip says what was not proven.
            AND THE TWO KERIPY VERSIONS ARE COMPARED FIRST. When the pins differ the handover is
            not a same-pin replacement at all but a migration crossing, which needs `kli migrate
            run` and is a one-way door (@a24p3kbw) — so the tests skip and say so, naming both
            versions, rather than failing in a way that reads as a broken upgrade. Rehearsing the
            migration itself remains unbuilt and remains the larger gap; this closes the smaller
            one and makes the larger one visible instead of implicit.

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

    This repo also hosts an issuer's Registrar  co-hosted with its witnesses = decision:
      id: r3aonvlz
      why: >
        ACDC revocation needs a verifier to learn TEL state without asking the issuer at
        verification time: "a registry consulted at verification time is a phone-home in
        disguise" (keri-bible 05 §9). The spec's answer is a Registrar under the Issuer and an
        Observer under the verifier, and Sam Smith presents that split as the witness/watcher
        governance structure replayed one layer up: controllers run witnesses, verifiers run
        watchers; issuers run Registrars, verifiers run Observers. So the Registrar lives beside
        the issuer's witnesses, in this repo, and the Observer lives with the verifier (heti).
        Neither keripy nor keri-foundation has an implementation or a wire protocol (keripy
        registraring.py is a docstring stub, keri-foundation/registrar a placeholder), so the
        protocol below is ours and says so; it is a SEDI Summit demo first (brief 6, Daniel's
        Q-CW26 and R-YSGN), not a claim to be the standard. Rejected putting the Registrar in imbu,
        which would merge issuing with publishing and make the issuer's API the thing a verifier
        subscribes to; rejected publishing to verifier-run infrastructure, which puts the issuer
        in the verifier's governance.
      children:

        The Registrar is its own process and never touches the witness = decision:
          id: 46t5otqe
          why: >
            @c7v3kp forbids anything that could stall or corrupt the served witness, so the
            Registrar runs as its own process role, `witness registrar`, with its own store, and
            reads nothing of the witness's LMDB. It is co-hosted with the witnesses in the sense of
            governance and deployment, not of address space. This makes the CLI four subcommands,
            amending @ixdaut53's three by @g3w6px's one-entry-point argument. Accepted tradeoff:
            one more process to supervise when an issuer runs both.

        The Registrar trusts its publisher and judges nothing cryptographically = decision:
          id: 5m2m4ozz
          why: >
            The Registrar accepts TEL snapshots (a registry's whole chain from rip to head, plus the
            issuer KEL that anchors it) only from publishers it is configured with, each
            authenticating by RFC 9421 via fiki (@s6v3qm); an unauthenticated POST is
            refused. It keeps a snapshot only if its chain extends the head already held, byte for
            byte, so a late or forked snapshot is refused with a named code and never replaces a
            newer head. It does NOT verify anchors against the issuer's KEL: the Observer must
            never rely on the Registrar's word (brief 6), so the verification that matters happens
            there, under the verifier's governance and with the keripy imbu and heti share. A second
            verification here, against this repo's own keripy pin (@w7c4mz), which is not heti's,
            could only add a way for two readings of one chain to disagree. Accepted tradeoff: a
            Registrar will
            relay a well-formed chain its issuer should not have published; the Observer refuses it.

        Observers subscribe once  and the Registrar pushes batches on a clock = decision:
          id: ed3dkgl5
          why: >
            An Observer subscribes once, naming a callback URL and signing with its own key, and
            may unsubscribe the same way. The Registrar then pushes; there is no pull, poll or
            per-registry query endpoint at all, so no verification anywhere can cause Registrar
            traffic (brief 6's falsifier counts those requests and wants zero). Pushes are batched
            on a clock of window W: each batch carries every registry whose head changed in the
            window, so an Observer cannot tell which change preceded which validation, and a
            change is not published the instant it happens. Batching is a privacy requirement, not
            a performance one: "if all the registries, 100% of them update at the same clock time"
            there is nothing to correlate (Sam Smith, The Digital Identity Tradespace).
          children:

            Every window sends one signed batch  empty when nothing changed = decision:
              id: 3m2eys6w
              why: >
                Daniel approved this 2026-09-24 (Q-ZGF7) as a TEMPORARY DEMO KLUDGE, to be replaced
                by a webhook, SSE or better (tick ~7nq3). A verifier refuses a head older than its
                freshness bound F, and if quiet windows sent nothing, an Observer could not tell
                "nothing changed" from "the Registrar is cut off": either valid credentials go stale,
                or blocking the Registrar freezes a revoked credential as valid. So every window W
                sends exactly one batch, signed by the Registrar's own key, carrying a monotonic
                batch number and the window's end time, empty when nothing changed. The Observer
                measures freshness from its last batch and treats a gap in batch numbers as a
                refusal condition. Constant-rate batches also hide WHETHER anything changed in a
                window. The transport sits behind one small seam, so replacing it is local.

            A subscription begins with a batch holding every head = decision:
              id: spxbqxaa
              why: >
                A batch carries only the registries that changed in its window, so an Observer that
                subscribes after issuance, or resubscribes after missing a batch, would never learn a
                head that does not change again. The first batch of every subscription therefore
                holds every head the Registrar has, and later batches carry changes only. This is
                also how an Observer recovers from a gap in batch numbers: it unsubscribes and
                subscribes again, which starts a new sequence with a full batch (heti this.i
                @4fgkgc3t). Repeating a live subscription does not restart it; see @o64bkgtl.
                Accepted tradeoff: one full-size batch per new subscription, whose size reveals how
                many registries the issuer has, which is public anyway.

            The built-in batch window is herd-privacy sized  and the demo overrides it = decision:
              id: xx6tjfxy
              why: >
                The window trades privacy against revocation latency: longer windows put more
                registries in each batch and make timing less informative, and shorter windows make
                a revocation take effect sooner. The Registrar's own default is sized for herd
                privacy, minutes rather than seconds (tick ~2db3 chooses the production value), and
                it is always configuration, never code. The SEDI Summit harness overrides it with a
                5-10 s stage value, because an audience cannot wait minutes. Rejected a stage-sized
                default, which would ship the weakest privacy setting to anyone who forgot to set it.

        A callback is an untrusted destination  and delivery is bounded = decision:
          id: qgacju62
          why: >
            Any signer may subscribe, so a callback URL is attacker-chosen, and delivering to it
            makes the Registrar an HTTP client on the attacker's behalf (Codex and Copilot on
            PR #19). So a callback must resolve only to public addresses unless the operator
            allows a host name or CIDR (the demo's Observer is on loopback, allowed explicitly in
            the harness config); the address checked is the address connected to; redirects are
            never followed. Subscriptions are capped in total, by configuration; each AID has at
            most one, because a subscription's identity is its AID. Batches are delivered
            concurrently, each under one wall-clock deadline of at most half the window, covering
            name resolution, connect, send and read together (a per-read socket timeout would let
            a trickling callback live on), and that deadline is its own setting, not the inbound
            freshness bound. A delivery past its deadline is abandoned, so one slow or dead
            callback cannot delay anyone else's heartbeat, and a failed delivery still spends its
            number. An abandoned thread cannot be killed (a resolver may never return), so no new
            delivery starts to a callback whose earlier one is still alive, which spends that
            window's number like any failure, and delivery threads alive at once, abandoned ones
            included, are capped globally; the check and the claim on a slot are one atomic step,
            so two concurrent sends cannot both pass it. A socket that a connect completes only
            after the deadline is closed at once rather than used. Admission is bounded the same
            way: resolving a callback at subscription time has its own deadline, and resolutions
            alive at once are capped, so a stalled resolver cannot hold a request thread. One failing
            subscriber, or one failing window, never ends the batch thread.
          children:

            A resolution holds its slot until it ends  and ends because it can be killed = decision:
              id: t3ju3fxz
              why: >
                Codex on PR #19 at 129f2fd (tick ~7onn) found three ways around the admission cap.
                The slot was released when its thread was tracked, but the thread started only
                afterwards, so a concurrent admission could prune it as dead and admit past the
                cap; a slot is now counted from reservation until the resolution returns, and
                released by the resolving thread itself. Admission ran before the replay check, so
                re-sending one accepted signed request made the Registrar resolve an
                attacker-chosen host again each time; a request already acted on, or identical to
                one still in admission, is now refused before anything is resolved. The replay
                record still commits with the subscription, so a request refused for any other
                reason can be retried. And a resolution that never returns held its slot until
                restart, because getaddrinfo cannot be interrupted from Python; sixteen of them
                closed admission for good. The host is now resolved in a child interpreter that
                is killed at a fixed timeout, so every slot is released in bounded time. Rejected
                an async resolver library (a new dependency, and resolvers such as dnspython skip
                /etc/hosts and nsswitch, which would change what an allowed host name means from
                what the operator sees with getaddrinfo). The same resolver serves delivery, whose
                abandoned threads had the same unbounded lifetime. Accepted tradeoff: an
                interpreter start per resolution, tens of milliseconds, at subscription time and
                once per subscriber per window.

        The Registrar's state survives a restart = decision:
          id: oxtmbdfq
          why: >
            Subscribers, the newest head per registry, and the batch counter persist in the
            Registrar's own store, so a restart neither forgets who to push to, accepts a stale
            snapshot it had already superseded, nor reuses a batch number an Observer has seen
            (which the Observer would read as a replay). Rejected in-memory state, which the demo's
            stand-in observer had and which F-K7N4 records as a defect.
          children:
            One Registrar per store  every read-modify-write in one transaction = decision:
              id: o64bkgtl
              why: >
                A thread lock cannot protect a store from a second process, and a read taken
                outside the write's transaction can be stale by the time it is used: two
                Registrars on one store could each accept a different extension of the same head
                or both issue batch number 1 (Codex on PR #19). So the Registrar holds an exclusive
                lock on its database file, resolved through any symlink so that two paths to one
                file are one store, for its lifetime and refuses to start without it, and every
                read-modify-write runs under BEGIN IMMEDIATE. Subscribing is idempotent: a
                repeated subscription, or one moving to a new callback, continues its sequence
                rather than restarting it, and a signed subscribe or unsubscribe already seen
                inside its freshness window is refused, keyed on signer, created time and the
                signature's decoded bytes, never the header text, whose label is unsigned. A
                sighting is kept for the verifier's whole acceptance window, max age plus the
                clock skew fiki tolerates, one shared constant, and the clock is read once per
                request: the verifier judges freshness and the store prunes at the same instant, so
                a sighting is never pruned while the verifier would still accept its request. A
                sighting is recorded in the same transaction as the subscribe or unsubscribe it
                protects, so a refused request leaves nothing behind and can be retried, and the
                sightings held are capped per signer and in total, so replay protection cannot
                be made to grow the store without bound.
                There is no migration for sightings recorded as header text by an earlier build:
                this PR introduces the Registrar, and no Registrar store has existed outside tests.
                Publications are exempt from that replay check because a replayed publication
                changes nothing (an exact repeat is re-acknowledged, anything older is stale) and a
                publisher retrying one snapshot produces byte-identical signatures. The state
                directory and database are owner-only, since they hold the signing seed.
            Every batch names the subscription it was sent for = decision:
              id: jpag4sof
              why: >
                A subscription carries a random nonce its Observer chooses, and every batch the
                Registrar signs for that subscription includes it as "subscription". Without it
                nothing in a signed batch said which subscription it belonged to, so a captured
                full batch 1 from an earlier subscription, still inside its signature's age, could
                be replayed to an Observer that had just resubscribed after a restart and close
                its gap with old heads (Codex on heti#21). The Observer refuses any batch whose
                nonce is not its current one. A subscribe must carry the nonce (16 to 64
                base64url characters) or it is refused; repeating a subscription replaces the
                nonce with the new one and, as before, continues the numbering.
