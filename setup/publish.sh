#!/usr/bin/env bash
# Secret-scan and push the private data repo.
#
# Runs as you, never as the agent. The agent commits its work; this decides what leaves
# the machine. Keeping the two separate means a manipulated agent — which from v1.5 will
# be reading text written by strangers — has no outbound path to the remote.
#
# See docs/trust-model.md (principals) and docs/egress.md (row 2).

set -euo pipefail

DATA_DIR="${LIFE_AGENT_DATA:?LIFE_AGENT_DATA is not set}"
cd "$DATA_DIR"

# Refuse to push a repo whose remote is public. Visibility is a two-click change on
# GitHub and this is the whole ballgame, so it is checked every single run rather than
# assumed from setup time.
REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$REMOTE_URL" ]]; then
    echo "no remote configured; nothing to publish" >&2
    exit 0
fi
if command -v gh &>/dev/null; then
    SLUG="$(sed -E 's#.*github\.com[:/]##; s#\.git$##' <<< "$REMOTE_URL")"
    VIS="$(gh repo view "$SLUG" --json visibility -q .visibility 2>/dev/null || echo UNKNOWN)"
    if [[ "$VIS" != "PRIVATE" ]]; then
        echo "REFUSING TO PUSH: $SLUG reports visibility=$VIS (expected PRIVATE)" >&2
        logger -t life-agent-publish "push refused, visibility=$VIS"
        exit 1
    fi
fi

# Anything uncommitted at this point is the agent's work from a run that died partway,
# or a hand edit. Commit it rather than pushing a partial state.
if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git status --porcelain)" ]]; then
    git add -A
    git commit -m "data: snapshot $(date +%F)" || true
fi

# Credentials live outside every repository, so a hit here means something went wrong —
# most likely a token or key pasted into a thread body without thinking.
if command -v gitleaks &>/dev/null; then
    if ! gitleaks protect --staged --no-banner --redact 2>/dev/null; then
        echo "gitleaks found a candidate secret; push aborted" >&2
        logger -t life-agent-publish "push aborted by gitleaks"
        exit 1
    fi
else
    echo "warning: gitleaks not installed, pushing without a secret scan" >&2
    logger -t life-agent-publish "gitleaks missing, scan skipped"
fi

git push origin HEAD
logger -t life-agent-publish "pushed $(git rev-parse --short HEAD)"
