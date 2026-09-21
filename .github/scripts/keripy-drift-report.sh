#!/usr/bin/env bash
# Turn the keripy drift canary's three measurements into something a person can act on holding
# nothing but the notification email (@7enojuyn).
#
# The canary's findings are genuinely hard to read cold: nothing is broken, no deploy is at risk,
# and the right response is often to do nothing yet. None of that is visible in a stack trace, so
# every outcome here writes a paragraph naming the situation, what is and is not at risk, what
# would clear it, and where the governing decision lives.
#
# That paragraph goes to three places. The job summary and the error annotation are one click from
# the failure mail. The GitHub issue is the one whose whole body arrives IN a mailbox, so it is the
# artifact that matters — filed when a condition needs a human, refreshed silently while it lasts
# (editing a body sends no mail, so a condition open for a month costs one message, not four), and
# closed by the run that finds it cleared.
#
# THE ORDER OF THE THREE PHASES BELOW IS LOAD-BEARING. Decide, then write locally, then talk to
# GitHub. An earlier draft filed the issue first, so a `gh` outage took the annotation and the
# summary down with it under `set -e` and left a bare "step failed" where the explanation should
# have been — losing the local report to protect the remote copy of it, which is backwards.
#
# Inputs arrive as environment variables from the workflow; see keripy-drift.yml.

set -euo pipefail

REPO="${GITHUB_REPOSITORY}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

# The measuring job's findings, which are UNTRUSTED DATA: upstream code ran in the job that
# produced them. They are quoted into prose and never evaluated, and they live outside the
# workspace so that nothing in them can be mistaken for code to run. Defaults to the working
# directory so the script can still be exercised by hand.
FINDINGS_DIR="${FINDINGS_DIR:-.}"
SUITE_LOG="${FINDINGS_DIR}/suite.log"
FORK_ONLY_COMMITS="$(cat "${FINDINGS_DIR}/fork-only-commits.txt" 2>/dev/null || echo '  (none recorded)')"

# `grep` exits 1 on no match, which under `pipefail` is indistinguishable from a failed read
# unless the file is checked separately — so a drift run whose failures do not happen to start
# with FAILED or ERROR would claim the log was unavailable. Distinguish the three cases.
suite_failures() {
    local found
    if [ ! -f "$SUITE_LOG" ]; then
        echo "(the test log did not reach this job — see the run log)"
        return 0
    fi
    found=$(grep -E '^(FAILED|ERROR)' "$SUITE_LOG" | sed -n '1,20p') || true
    if [ -n "$found" ]; then
        printf '%s\n' "$found"
    else
        echo "(no FAILED/ERROR lines in the log — see the run log)"
    fi
}
if [ -n "${TRACKING_PR:-}" ]; then
    PR_REF="${UPSTREAM}#${TRACKING_PR}"
else
    PR_REF="not identified"
fi

DECISIONS=$(
    cat <<'EOF'
## Where the reasoning lives, if you want it

- `this.i` → `@w7c4mz` — why keripy is an unforked, pinned dependency at all.
- `this.i` → `@enyp5khx` — why a fork-only pin is allowed only while an upstream PR carries it.
- `this.i` → `@7enojuyn` — why this issue exists rather than just a red build.
- `tick show 4ky2` — the standing task to re-pin once the upstream PR resolves.
- `.github/workflows/keripy-drift.yml` — the job itself, commented.
EOF
)

# =============================================================================================
# Phase 1 — decide. Pure: reads the environment, touches no network.
# =============================================================================================

# pytest's exit status is five-valued and only ONE value means "tests failed": 0 passed,
# 1 failed, 2 interrupted, 3 internal error, 4 usage error, 5 nothing collected. Collapsing
# 2-5 into "failed" is how a mistyped marker or a broken runner gets reported as a confident
# verdict about upstream keripy — drift that never happened, or a clean week that was never
# measured. So classify, and treat anything above 1 as "we do not know".
verdict_of() {
    case "${1:-}" in
    '') echo absent ;;
    0) echo clean ;;
    1) echo failed ;;
    *) echo unmeasured ;;
    esac
}

suite_verdict=$(verdict_of "${SUITE_RC:-}")
landed_verdict=$(verdict_of "${LANDED_RC:-}")

# Retryability is COMPUTED, following the same reasoning copilot-review-gate.yml sets out: a
# usage error or an empty selection is a broken invocation that will break identically next
# week, while an interruption or a half-finished setup may well not. Telling an operator not to
# retry something a retry would fix is worse than saying nothing.
inconclusive_code="e.self.unknown.r"
for rc in "${SUITE_RC:-}" "${LANDED_RC:-}"; do
    case "$rc" in
    4 | 5) inconclusive_code="e.self.config.canary.f" ;;
    esac
done

inconclusive=0
# The measuring job's own verdict, passed explicitly rather than inferred from which outputs
# happen to be empty. `TRACKING_PR` is legitimately empty when no PR exists, so absence is a
# genuinely ambiguous signal here and guessing from it is how a job ends up reporting a
# conclusion nobody measured.
case "${MEASURE_RESULT:-success}" in
success) ;;
*) inconclusive=1 ;;
esac
if [ -z "${PINNED:-}" ] || [ "$suite_verdict" = absent ] || [ "$suite_verdict" = unmeasured ]; then
    inconclusive=1
fi
# Only a fork-only pin owes these two answers. When the pin is upstream the PR search and the
# inverse canary are both skipped, and their absence is the correct state rather than a gap.
if [ "${FORK_ONLY:-}" = "true" ]; then
    if [ "${TRACKING_KNOWN:-}" != "true" ]; then
        inconclusive=1
    fi
    case "$landed_verdict" in
    clean | failed) ;;
    *) inconclusive=1 ;;
    esac
fi

# Landed outranks untracked, and the case that makes it matter is the ordinary one. When
# upstream takes our work by squash or cherry-pick, our pinned SHA is still not an ancestor of
# their main AND the PR that carried it is now closed — so the untracked test fires, and the
# week our change is accepted we would send an alarm saying nobody is carrying it. Same facts,
# opposite meanings; the landed measurement is the one that knows which.
landed=0
untracked=0
drift=0
if [ "$inconclusive" = 0 ]; then
    [ "$landed_verdict" = clean ] && landed=1
    if [ "${FORK_ONLY:-}" = "true" ] && [ -z "${TRACKING_PR:-}" ] && [ "$landed" = 0 ]; then
        untracked=1
    fi
    [ "$suite_verdict" = failed ] && drift=1
fi

# =============================================================================================
# Phase 2 — write the report where nothing can take it away. No network yet.
# =============================================================================================

if [ "$inconclusive" = 1 ]; then
    {
        echo "## ⚠️ The canary could not run"
        echo
        echo "This is a broken canary, **not** a report about keripy. Something before or during"
        echo "the measurement did not complete — read the log above for which step. Nothing was"
        echo "measured, so no conclusion about upstream drift should be drawn from this run, in"
        echo "either direction."
        echo
        echo "No issue was filed, deliberately: there is nothing yet to say."
        echo
        echo "\`\`\`"
        echo "measure job  : ${MEASURE_RESULT:-<unknown>}"
        echo "pinned       : ${PINNED:-<unread>}"
        echo "suite exit   : ${SUITE_RC:-<never ran>}  (${suite_verdict})"
        echo "inverse exit : ${LANDED_RC:-<never ran>}  (${landed_verdict})"
        echo "pr search    : ${TRACKING_KNOWN:-<never completed>}"
        echo "\`\`\`"
    } >> "$SUMMARY"
    echo "::error::[${inconclusive_code}] The keripy drift canary could not complete its" \
        "measurements, so it is reporting nothing rather than guessing. This says nothing about" \
        "upstream keripy, in either direction."
    exit 1
fi

if [ "$untracked" = 1 ]; then
    cat > untracked.md <<EOF
## What this email means

witness is pinned to a keripy commit that does **not** exist upstream. In plain terms: **we are
using keripy features that live only in our own fork, and no open pull request is carrying them
home.** Nothing but memory is keeping that change alive — if everyone who knows about it stops
thinking about it, witness quietly becomes a project that depends on a private fork forever.

|  |  |
| --- | --- |
| Pinned commit | \`${FORK}@${PINNED}\` |
| Upstream head | \`${UPSTREAM}@${UPSTREAM_HEAD}\` |
| Open upstream PR carrying it | **none — this is the problem** |

Commits we carry that \`${UPSTREAM}\` does not:

${FORK_ONLY_COMMITS}

## Is anything broken?

No. \`main\` is fine, CI is fine, the published image is fine. This is about **divergence**, not
breakage. It is safe to leave until you have half an hour.

## What clears it

Either one, and the next run closes this issue by itself:

1. Open a PR to \`${UPSTREAM}\` from the branch holding those commits, so upstream review is
   under way; or
2. drop the dependency on the fork-only code and re-pin witness to an upstream commit.

${DECISIONS}

<sub>Filed automatically by the keripy drift canary · [run log](${RUN_URL})</sub>
EOF
    echo "::error::[e.rule.fork-pin-untracked.f] witness depends on keripy code that exists only" \
        "on the bakobo fork, and no open upstream PR is carrying it home. Nothing is broken; this" \
        "is about divergence. The issue this run filed explains it in full."
    {
        echo "## ❌ Fork-only pin, untracked"
        cat untracked.md
    } >> "$SUMMARY"
fi

if [ "$landed" = 1 ]; then
    cat > landed.md <<EOF
## What this email means

**Good news, with a chore attached.** The keripy features witness was carrying on the bakobo fork
now exist in \`${UPSTREAM}\` — the tests marked \`fork_only_keripy\`, which are supposed to fail
against upstream, passed there. Upstream has taken the change.

|  |  |
| --- | --- |
| Pinned commit (still the fork) | \`${FORK}@${PINNED}\` |
| Upstream head (now has the feature) | \`${UPSTREAM}@${UPSTREAM_HEAD}\` |
| Upstream PR | ${PR_REF} |

## Is anything broken?

No, and nothing is urgent. witness keeps working on the fork pin indefinitely. What is true is
that the reason for the fork pin has expired, so every week it stays is a week of divergence
nobody needs.

## What clears it

Re-pin \`keri\` in \`pyproject.toml\` to the upstream commit, run \`uv lock\`, and drop the
\`fork_only_keripy\` marker from any test that no longer needs it. Check first whether upstream
**renamed** anything on the way in — if the store or route spellings changed, the contract tests
are what will tell you, and \`witness.reader._declared\` is what has to follow.

${DECISIONS}

<sub>Filed automatically by the keripy drift canary · [run log](${RUN_URL})</sub>
EOF
    echo "::error::[e.state.pin-superseded.f] Our fork-only keripy commits have landed upstream." \
        "Nothing is broken; the fork-only pin is simply no longer necessary and should come home." \
        "The issue this run filed explains it in full."
    {
        echo "## ❌ Fork-only features landed upstream — re-pin"
        cat landed.md
    } >> "$SUMMARY"
fi

if [ "$drift" = 1 ]; then
    cat > drift.md <<EOF
## What this email means

**Upstream keripy has changed under us.** witness reads several semi-internal keripy accessors
(\`db.fels\`, \`db.clonePreIter\`, the stores beside them) rather than forking keripy, and this
job runs the contract tests that pin their behaviour against \`${UPSTREAM}\` every Monday. This
week they did not all pass. Something upstream moved.

|  |  |
| --- | --- |
| What witness is pinned to (unchanged) | \`${FORK}@${PINNED}\` |
| What broke against | \`${UPSTREAM}@${UPSTREAM_HEAD}\` |

Failures:

\`\`\`
$(suite_failures)
\`\`\`

## Is anything broken *now*?

No. This is the entire point of the canary: witness is pinned, so production, \`main\` and the
published image all still run the keripy they were built against. What has happened is that the
**next** pin bump just got more expensive, and you are finding out while the change is small
instead of six months later.

## What clears it

Read the failures and decide, which is a judgement call and not a chore:

1. **Follow upstream** — adapt witness to the new shape, bump the pin, update the contract test
   so it characterises the new behaviour; or
2. **Don't** — if upstream's change is wrong or not for us, record why, and the contract test
   becomes the note explaining what we are declining to follow.

Either way the tests in \`tests/test_keripy_contract.py\` are where the decision gets written
down. The next green run closes this issue.

${DECISIONS}

<sub>Filed automatically by the keripy drift canary · [run log](${RUN_URL})</sub>
EOF
    echo "::error::[e.env.keripy.drifted.f] Upstream keripy moved under the accessors witness" \
        "rides. Nothing is broken right now — the pin is unchanged and production is unaffected." \
        "This is early warning that the next pin bump costs more. See the issue this run filed."
    {
        echo "## ❌ Upstream drift"
        cat drift.md
    } >> "$SUMMARY"
fi

if [ "$untracked$landed$drift" = "000" ]; then
    {
        echo "## ✅ No action needed"
        echo
        echo "The contract tests pass against \`${UPSTREAM}@${UPSTREAM_HEAD}\`, so nothing upstream"
        echo "has moved under the accessors witness rides."
        echo
        if [ "${FORK_ONLY:-}" = "true" ]; then
            echo "witness is still pinned to fork-only keripy code (\`${FORK}@${PINNED}\`), which is"
            echo "allowed because ${PR_REF} is open and carrying it home. That PR is the thing to"
            echo "chase; this job will say so the week it stops being open."
        else
            echo "The pin is an ancestor of upstream \`main\`, so witness carries no fork-only keripy."
        fi
        echo
        echo "$DECISIONS"
    } >> "$SUMMARY"
    echo "No action needed."
fi

# =============================================================================================
# Phase 3 — sync the issues. Everything above is already written, so a GitHub failure here costs
# the mailed copy of the report and not the report.
# =============================================================================================

sync_failed=0

note_sync_failure() {
    sync_failed=1
    echo "::warning::[e.env.github.unreadable.r] The canary's verdict is in the job summary" \
        "above, but it could not $1 the GitHub issue that carries that verdict into a mailbox." \
        "The verdict itself stands. Run the job again to sync the issue."
}

# Scoped by LABEL, not by page size. The dedup search was `--limit 100` over every open issue,
# which is a lookup that silently stops being correct as the repo fills up: past a hundred, an
# actionable run files a duplicate instead of refreshing, and a clean run fails to close the
# stale one. The canary owns at most three issues ever, so labelling them bounds the set to
# three by construction and the page size stops mattering. It also gives a human one filter for
# "everything this job has ever said".
#
# The invariant that makes this safe, and it is worth stating because it will not be obvious
# later: the label arrives in the SAME change as the issue-filing itself, so there has never
# been an unlabelled generation to strand. If this scoping were ever bolted onto a job that had
# already filed issues, those would be invisible to every lookup — duplicated instead of
# refreshed, and never closable — so that variant would owe a one-time relabel first.
#
# No `head -1` in this pipeline either: it closes the pipe on jq, and under `pipefail` that
# SIGPIPE would read as a failed lookup. jq picks the first match itself.
ISSUE_LABEL="keripy-drift"

ensure_label() {
    gh label create "$ISSUE_LABEL" --repo "$REPO" --force --color d4c5f9 \
        --description "Filed by the keripy drift canary; closed by it when the condition clears" \
        > /dev/null
}

open_issue_number() {
    gh issue list --repo "$REPO" --state open --label "$ISSUE_LABEL" --limit 100 \
        --json number,title |
        jq -r --arg t "$1" 'map(select(.title == $t)) | .[0].number // empty'
}

# File it if it is new; refresh it silently if it is already open. Never comment on each run.
raise() {
    local title="$1" body_file="$2" existing
    if ! existing=$(open_issue_number "$title"); then
        note_sync_failure "look up"
        return 0
    fi
    if [ -n "$existing" ]; then
        gh issue edit "$existing" --repo "$REPO" --body-file "$body_file" ||
            note_sync_failure "refresh"
    else
        # The label has to exist before an issue can carry it, and `--force` makes that
        # idempotent. Only needed on the create path; a refresh or a close already has it.
        if ! ensure_label; then
            note_sync_failure "label"
            return 0
        fi
        gh issue create --repo "$REPO" --title "$title" --body-file "$body_file" \
            --label "$ISSUE_LABEL" || note_sync_failure "file"
    fi
}

clear_issue() {
    local title="$1" why="$2" existing
    if ! existing=$(open_issue_number "$title"); then
        note_sync_failure "look up"
        return 0
    fi
    if [ -n "$existing" ]; then
        gh issue close "$existing" --repo "$REPO" --comment "$why" || note_sync_failure "close"
    fi
}

if [ "$untracked" = 1 ]; then
    raise "$UNTRACKED_TITLE" untracked.md
else
    clear_issue "$UNTRACKED_TITLE" \
        "Cleared: the pin is either upstream now, or an open upstream PR is carrying it home."
fi

if [ "$landed" = 1 ]; then
    raise "$LANDED_TITLE" landed.md
else
    clear_issue "$LANDED_TITLE" "Cleared: the fork-only tests no longer pass against upstream."
fi

if [ "$drift" = 1 ]; then
    raise "$DRIFT_TITLE" drift.md
else
    clear_issue "$DRIFT_TITLE" "Cleared: the contract tests pass against upstream keripy again."
fi

if [ "$untracked$landed$drift" != "000" ] || [ "$sync_failed" = 1 ]; then
    exit 1
fi
exit 0
