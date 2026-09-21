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
# Inputs arrive as environment variables from the workflow; see keripy-drift.yml.

set -euo pipefail

REPO="${GITHUB_REPOSITORY}"
FORK_ONLY_COMMITS="$(cat fork-only-commits.txt 2>/dev/null || echo '  (none recorded)')"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
if [ -n "${TRACKING_PR:-}" ]; then
    PR_REF="${UPSTREAM}#${TRACKING_PR}"
else
    PR_REF="not identified"
fi

# A step before the measurements failed — the resolve broke, the checkout broke, the pin would
# not parse. Say THAT and stop: filing "upstream drifted" off a suite that never ran would be a
# confident lie, and confident lies are what @7enojuyn exists to stop.
if [ -z "${PINNED:-}" ] || [ -z "${SUITE_RC:-}" ] ||
    { [ "${FORK_ONLY:-}" = "true" ] && [ "${TRACKING_KNOWN:-}" != "true" ]; }; then
    {
        echo "## ⚠️ The canary could not run"
        echo
        echo "This is a broken canary, **not** a report about keripy. One of the steps before the"
        echo "test run failed — read the log above for which. Nothing has been measured, so no"
        echo "conclusion about upstream drift should be drawn from this run, in either direction."
        echo
        echo "No issue was filed, deliberately: there is nothing yet to say."
    } >> "$SUMMARY"
    echo "::error title=The keripy drift canary could not run::A step before the test run failed," \
        "so nothing was measured. This says nothing about upstream keripy."
    exit 1
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

open_issue_number() {
    gh issue list --repo "$REPO" --state open --limit 100 --json number,title |
        jq -r --arg t "$1" '.[] | select(.title == $t) | .number' | head -1
}

# File it if it is new; refresh it silently if it is already open. Never comment on each run.
raise() {
    local title="$1" body_file="$2" existing
    existing=$(open_issue_number "$title")
    if [ -n "$existing" ]; then
        gh issue edit "$existing" --repo "$REPO" --body-file "$body_file"
        echo "Refreshed existing issue #${existing}."
    else
        gh issue create --repo "$REPO" --title "$title" --body-file "$body_file"
    fi
}

clear_issue() {
    local title="$1" why="$2" existing
    existing=$(open_issue_number "$title")
    if [ -n "$existing" ]; then
        gh issue close "$existing" --repo "$REPO" --comment "$why"
        echo "Closed issue #${existing}: ${why}"
    fi
}

failed=0

# --- Question 1: a fork-only pin with nothing carrying it home -------------------------------

if [ "${FORK_ONLY:-}" = "true" ] && [ -z "${TRACKING_PR:-}" ]; then
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
    raise "$UNTRACKED_TITLE" untracked.md
    echo "::error title=Fork-only keripy pin with no upstream PR::witness depends on keripy code" \
        "that exists only on the bakobo fork, and nothing is carrying it upstream. Nothing is" \
        "broken; this is about divergence. See the issue this run filed."
    {
        echo "## ❌ Fork-only pin, untracked"
        cat untracked.md
    } >> "$SUMMARY"
    failed=1
else
    clear_issue "$UNTRACKED_TITLE" \
        "Cleared: the pin is either upstream now, or an open upstream PR is carrying it home."
fi

# --- Question 3: the fork-only feature has landed upstream ------------------------------------

if [ "${LANDED_RC:-1}" = "0" ]; then
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
    raise "$LANDED_TITLE" landed.md
    echo "::error title=Our fork-only keripy commits have landed upstream::Nothing is broken." \
        "The fork-only pin is no longer necessary and should come home. See the issue this run filed."
    {
        echo "## ❌ Fork-only features landed upstream — re-pin"
        cat landed.md
    } >> "$SUMMARY"
    failed=1
else
    clear_issue "$LANDED_TITLE" "Cleared: the fork-only tests no longer pass against upstream."
fi

# --- Question 2: real upstream drift ----------------------------------------------------------

if [ "${SUITE_RC:-1}" != "0" ]; then
    cat > drift.md <<EOF
## What this email means

**Upstream keripy has changed under us.** witness reads several semi-internal keripy accessors
(\`db.fels\`, \`db.clonePreIter\`, the stores beside them) rather than forking keripy, and this
job runs the contract tests that pin their behaviour against \`${UPSTREAM}\` \`main\` every
Monday. This week they did not all pass. Something upstream moved.

|  |  |
| --- | --- |
| What witness is pinned to (unchanged) | \`${FORK}@${PINNED}\` |
| What broke against | \`${UPSTREAM}@${UPSTREAM_HEAD}\` |

Failures:

\`\`\`
$(grep -E '^(FAILED|ERROR)' suite.log 2>/dev/null | head -20 || echo 'see the run log')
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
    raise "$DRIFT_TITLE" drift.md
    echo "::error title=Upstream keripy moved under witness's contract tests::Nothing is broken" \
        "right now — the pin is unchanged and production is unaffected. This is early warning that" \
        "the next pin bump costs more. See the issue this run filed."
    {
        echo "## ❌ Upstream drift"
        cat drift.md
    } >> "$SUMMARY"
    failed=1
else
    clear_issue "$DRIFT_TITLE" "Cleared: the contract tests pass against upstream keripy again."
fi

# --- The quiet case ---------------------------------------------------------------------------

if [ "$failed" = "0" ]; then
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

exit "$failed"
