#!/usr/bin/env bash
# Create the unprivileged agent user and set the filesystem boundaries described in
# docs/trust-model.md.
#
# SCOPE — this script touches exactly three things:
#   1. creates a system user `life-agent` with no login shell and no sudo rights
#   2. sets ownership and modes on the code and data directories
#   3. installs the systemd timer units from setup/systemd/
#
# It does NOT touch networking, firewall rules, packages, or any existing system unit.
# That restraint is deliberate: on a headless host with no physical access, a setup script
# that reconfigures the network is a setup script that can end the machine.
#
# Runs in dry-run mode by default. Pass --apply to actually make changes.

set -euo pipefail

AGENT_USER="life-agent"
CODE_DIR="${CODE_DIR:-$HOME/life-agent}"
DATA_DIR="${DATA_DIR:-$HOME/life-agent-data}"
CONF_DIR="${CONF_DIR:-$HOME/.config/life-agent}"
OWNER="$(id -un)"

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
[[ $APPLY -eq 0 ]] && echo "  MODE  : dry run (pass --apply to make changes)"
echo

for d in "$CODE_DIR" "$DATA_DIR"; do
    [[ -d "$d" ]] || { echo "error: $d does not exist"; exit 1; }
done

echo "[1/4] agent user"
if id "$AGENT_USER" &>/dev/null; then
    echo "  user $AGENT_USER already exists, leaving alone"
else
    run sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$AGENT_USER"
fi
run sudo usermod -aG "$AGENT_USER" "$OWNER"

echo "[2/4] code directory — agent reads and executes, never writes"
run sudo chown -R "$OWNER:$OWNER" "$CODE_DIR"
run sudo chmod -R u=rwX,go=rX "$CODE_DIR"

echo "[3/4] data directory — scoped per subdirectory"
run sudo chown -R "$OWNER:$AGENT_USER" "$DATA_DIR"
run sudo chmod 750 "$DATA_DIR"
# threads/ is the shared collaboration surface: agent and owner both write.
run sudo chmod 770 "$DATA_DIR/threads"
run sudo chmod 770 "$DATA_DIR/briefs"
# setgid so files created by either principal stay group-accessible to the other.
run sudo chmod g+s "$DATA_DIR/threads" "$DATA_DIR/briefs"
# config.yaml: yours to write, agent may only read.
[[ -f "$DATA_DIR/config.yaml" ]] && run sudo chmod 640 "$DATA_DIR/config.yaml"
# The git metadata stays owner-only: the agent commits via the CLI, but must not be able
# to rewrite history or add a remote.
run sudo chmod -R o-rwx "$DATA_DIR/.git"

echo "[4/4] credentials — outside every repository, never group-readable"
run mkdir -p "$CONF_DIR"
run chmod 700 "$CONF_DIR"
echo "  place google-oauth.json and api-key here yourself, mode 0600,"
echo "  then grant read with a targeted ACL:"
echo "    setfacl -m u:$AGENT_USER:r $CONF_DIR/google-oauth.json $CONF_DIR/api-key"
echo "    setfacl -m u:$AGENT_USER:x $CONF_DIR"

echo
echo "systemd units in setup/systemd/ are TEMPLATES containing __OWNER__, __CODE_DIR__,"
echo "__DATA_DIR__ and __CONF_DIR__ placeholders. Substitute them before installing —"
echo "see the sed loop in setup/README.md — then:"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now life-agent-brief.timer life-agent-publish.timer life-agent-deadman.timer"
echo
echo "verify the boundaries actually hold:"
echo "  sudo -l -U $AGENT_USER              # must report no sudo rights"
echo "  sudo -u $AGENT_USER touch $CODE_DIR/x   # must fail: agent cannot write its own code"
echo "  sudo -u $AGENT_USER touch $DATA_DIR/threads/x  # must succeed"
[[ $APPLY -eq 0 ]] && echo && echo "dry run complete — nothing was changed."
