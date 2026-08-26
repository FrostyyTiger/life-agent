#!/usr/bin/env bash
# Create the unprivileged agent user and set the filesystem boundaries described in
# docs/trust-model.md, extended for mail-v1's isolation model (docs/plans/mail-v1.md).
#
# SCOPE — this script touches exactly these things:
#   1. creates a system user `life-agent`, with a real home (mail-v1 needs one — see
#      below), no login shell, no sudo rights
#   2. sets ownership and modes on the code, data, and mail-state directories
#   3. creates /run/life-agent via tmpfiles.d (the mail query socket's home)
#   4. sets traverse-only ACLs so life-agent can reach $CONF_DIR and the `claude`
#      binary under the owner's home, and chowns the three mail credential files to
#      life-agent if they exist
#   5. gives life-agent a git identity and write access to the data repo, so
#      `mail digest` can commit into a repo it doesn't own (see digest.git_commit_data_repo)
#   6. installs the systemd timer/service units from setup/systemd/
#
# It does NOT touch networking, firewall rules, packages, or any existing system unit.
# That restraint is deliberate: on a headless host with no physical access, a setup
# script that reconfigures the network is a setup script that can end the machine.
#
# Runs in dry-run mode by default. Pass --apply to actually make changes.

set -euo pipefail

AGENT_USER="life-agent"
AGENT_HOME="/var/lib/life-agent"
CODE_DIR="${CODE_DIR:-$HOME/life-agent}"
DATA_DIR="${DATA_DIR:-$HOME/life-agent-data}"
CONF_DIR="${CONF_DIR:-$HOME/.config/life-agent}"
STATE_DIR="${STATE_DIR:-$HOME/life-agent-state}"
OWNER="$(id -un)"
OWNER_HOME="$HOME"

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

run() {
    if [[ $APPLY -eq 1 ]]; then
        echo "  + $*"
        "$@"
    else
        echo "  would run: $*"
    fi
}

echo "life-agent bootstrap"
echo "  owner : $OWNER"
echo "  code  : $CODE_DIR"
echo "  data  : $DATA_DIR"
echo "  conf  : $CONF_DIR"
echo "  state : $STATE_DIR   (mail-v1: mail.db, HF model cache, claude -p's cwd/HOME)"
[[ $APPLY -eq 0 ]] && echo "  MODE  : dry run (pass --apply to make changes)"
echo

for d in "$CODE_DIR" "$DATA_DIR"; do
    [[ -d "$d" ]] || { echo "error: $d does not exist"; exit 1; }
done

echo "[1/7] agent user"
if id "$AGENT_USER" &>/dev/null; then
    echo "  user $AGENT_USER already exists, leaving alone"
else
    # --create-home (not the original --no-create-home): mail-v1's claude -p calls and
    # the HF embedding cache need a writable HOME. The systemd units still point
    # claude -p's actual cwd/HOME at $STATE_DIR/claude-cwd and $STATE_DIR/home — this
    # home directory just needs to exist so useradd/PAM/etc. don't trip over its absence.
    run sudo useradd --system --home-dir "$AGENT_HOME" --create-home \
        --shell /usr/sbin/nologin "$AGENT_USER"
fi
run sudo usermod -aG "$AGENT_USER" "$OWNER"

echo "[2/7] code directory — agent reads and executes, never writes"
run sudo chown -R "$OWNER:$OWNER" "$CODE_DIR"
run sudo chmod -R u=rwX,go=rX "$CODE_DIR"

echo "[3/7] data directory — scoped per subdirectory"
run sudo chown -R "$OWNER:$AGENT_USER" "$DATA_DIR"
run sudo chmod 750 "$DATA_DIR"
# threads/ and briefs/ are shared collaboration surfaces: agent and owner both write
# (briefs/ is where the mail digest lands).
run sudo chmod 770 "$DATA_DIR/threads"
run sudo chmod 770 "$DATA_DIR/briefs"
run sudo chmod g+s "$DATA_DIR/threads" "$DATA_DIR/briefs"
# config.yaml: yours to write, agent may only read.
[[ -f "$DATA_DIR/config.yaml" ]] && run sudo chmod 640 "$DATA_DIR/config.yaml"
# mail-feedback.jsonl lives in DATA_DIR's root, which is 750 (group r-x, not w) — the
# agent can't create a new file there. Pre-create it group-writable so `feedback.py`
# can append to the existing file (which needs only file-level write, not directory
# write) without ever needing to create it itself.
run sudo touch "$DATA_DIR/mail-feedback.jsonl"
run sudo chown "$OWNER:$AGENT_USER" "$DATA_DIR/mail-feedback.jsonl"
run sudo chmod 660 "$DATA_DIR/mail-feedback.jsonl"
# .git itself needs to be group-writable for `mail digest`'s commit (life-agent is in
# the group, not the owner) — chmod -R o-rwx above already stripped "other"; this adds
# "group" write/traverse without touching "other" again, plus setgid on its directories
# so new objects git creates stay group-owned rather than defaulting to life-agent's
# primary group in a way that could still exclude the owner.
run sudo chmod -R o-rwx "$DATA_DIR/.git"
run sudo chmod -R g+rwX "$DATA_DIR/.git"
run sudo find "$DATA_DIR/.git" -type d -exec chmod g+s {} +

echo "[4/7] mail state directory — life-agent only, no group bits, not even you"
# Deliberately no group access here (unlike threads/briefs above): the owner's account
# — and therefore every other Claude Code session on this host — must not be able to
# read mail.db or its tokens without sudo. See docs/trust-model.md's mail-specific
# prohibition.
run sudo mkdir -p "$STATE_DIR"
run sudo chown -R "$AGENT_USER:$AGENT_USER" "$STATE_DIR"
run sudo chmod 700 "$STATE_DIR"

echo "[5/7] /run/life-agent — the query socket's home, recreated on every boot"
TMPFILES_CONF="/etc/tmpfiles.d/life-agent-mail.conf"
run sudo tee "$TMPFILES_CONF" <<EOF
d /run/life-agent 0750 $AGENT_USER $AGENT_USER -
EOF
run sudo systemd-tmpfiles --create "$TMPFILES_CONF"

echo "[6/7] mail credentials + claude binary traversal"
run mkdir -p "$CONF_DIR"
run chmod 700 "$CONF_DIR"
# life-agent needs to walk down to $CONF_DIR and to wherever `claude` lives under your
# home (nvm installs there) — traverse-only, not read, so this grants no visibility
# into anything else in your home directory.
run sudo setfacl -m "u:$AGENT_USER:x" "$OWNER_HOME"
run sudo setfacl -m "u:$AGENT_USER:x" "$CONF_DIR"

for token in gmail-readonly-token.json gmail-insert-token.json claude-oauth-token; do
    path="$CONF_DIR/$token"
    if [[ -f "$path" ]]; then
        run sudo chown "$AGENT_USER:$AGENT_USER" "$path"
        run sudo chmod 0600 "$path"
    else
        echo "  $path does not exist yet — NEED-MARCEL: see docs/status/mail-v1.md"
    fi
done
echo "  google-client.json stays owned by you (life-agent never authenticates itself,"
echo "  it only uses tokens you already minted) — no ACL needed for it specifically"
echo "  beyond the $CONF_DIR traversal grant above."

echo "[7/7] git identity for life-agent — mail digest commits into a repo it doesn't own"
GITCONFIG_PATH="$AGENT_HOME/.gitconfig"
run sudo tee "$GITCONFIG_PATH" <<EOF
[safe]
	directory = $DATA_DIR
[user]
	name = life-agent
	email = life-agent@localhost
EOF
run sudo chown "$AGENT_USER:$AGENT_USER" "$GITCONFIG_PATH"
run sudo chmod 0644 "$GITCONFIG_PATH"
echo "  without this, git refuses the commit — 'dubious ownership' (the repo is owned"
echo "  by $OWNER, not life-agent) plus no configured identity to commit as."

echo
echo "systemd units in setup/systemd/ are TEMPLATES containing __OWNER__, __CODE_DIR__,"
echo "__DATA_DIR__, __CONF_DIR__, __STATE_DIR__ and __CLAUDE_BIN_DIR__ placeholders."
echo "Substitute them before installing — see the sed loop in setup/README.md — then:"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now life-agent-publish.timer life-agent-deadman.timer"
echo "  sudo systemctl enable --now life-agent-mail-sync.timer life-agent-mail-tag.timer \\"
echo "                              life-agent-mail-digest.timer life-agent-mail-query.service"
echo "  (life-agent-brief.timer is NOT enabled here — its job, src/main.py, was never"
echo "  implemented; see src/README.md. Enable it only once that code exists.)"
echo
echo "verify the boundaries actually hold — every line here is a claim in the trust"
echo "model, so every line is a command:"
echo "  sudo -l -U $AGENT_USER                        # must report no sudo rights"
echo "  sudo -u $AGENT_USER touch $CODE_DIR/x          # must fail: agent cannot write its own code"
echo "  sudo -u $AGENT_USER touch $DATA_DIR/threads/x  # must succeed"
echo "  cat $STATE_DIR/mail.db                         # must fail for you: Permission denied"
echo "  cat $CONF_DIR/gmail-readonly-token.json        # must fail for you: Permission denied"
echo "  systemctl list-timers 'life-agent-*'           # publish + deadman + three mail timers (five)"
echo "  systemctl is-active life-agent-mail-query      # must be 'active'"
[[ $APPLY -eq 0 ]] && echo && echo "dry run complete — nothing was changed."
