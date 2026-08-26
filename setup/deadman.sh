#!/usr/bin/env bash
# Dead-man's switch. Asserts that today's mail digest exists and is non-empty; notifies
# only when it does not.
#
# This is the simplest component in the system on purpose. It stats a file. It does not
# import the agent's code, does not call the model API, does not read a thread, and does
# not parse anything. Nothing watches the watchdog, so the watchdog must have almost no
# way to be wrong.
#
# Checks briefs/<today>-mail.md (mail-v1's digest — see docs/plans/mail-v1.md stage 8),
# not the calendar brief this script originally watched: the calendar-brief job
# (life-agent-brief.timer -> src/main.py) was never implemented — mail-v1 is what
# actually runs on this host. If the calendar system is built later, this check should
# grow to cover both files rather than pick one.
#
# The notification payload is a fixed string. It contains no thread content, no mail
# content, and no personal information — only the fact that a file is missing. See
# docs/egress.md row 4.

set -uo pipefail

DATA_DIR="${LIFE_AGENT_DATA:?LIFE_AGENT_DATA is not set}"
NTFY_TOPIC_FILE="${HOME}/.config/life-agent/ntfy-topic"
TODAY="$(date +%F)"
BRIEF="${DATA_DIR}/briefs/${TODAY}-mail.md"

notify() {
    local msg="$1"
    logger -t life-agent-deadman "$msg"
    echo "$msg" >&2
    if [[ -r "$NTFY_TOPIC_FILE" ]]; then
        curl -fsS --max-time 20 \
             -H "Title: life-agent" \
             -H "Priority: high" \
             -d "$msg" \
             "https://ntfy.sh/$(< "$NTFY_TOPIC_FILE")" >/dev/null \
            || logger -t life-agent-deadman "notification delivery failed"
    fi
}

if [[ ! -s "$BRIEF" ]]; then
    notify "No mail digest for ${TODAY}. The morning job did not produce output."
    exit 1
fi

logger -t life-agent-deadman "mail digest for ${TODAY} present ($(stat -c%s "$BRIEF") bytes)"
exit 0
