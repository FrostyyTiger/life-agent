#!/usr/bin/env bash
# Install the personal-host assistant setup on the VM from the two repo clones.
#
#   install.sh --user     links only, no sudo (safe to run any time; run nightly by assistant-update)
#   install.sh --system   also renders systemd units, the Samba config and /usr/local/sbin helpers (sudo)
#
# Expects ~/assistant/life-agent (public) and ~/assistant/life-agent-notes (private, holds host/MAP.md).
# Existing files that would be replaced by a link are kept as <file>.bak-<date>. Idempotent.
set -euo pipefail
MODE="${1:---user}"
HOST_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"   # life-agent/host
VM="$HOST_DIR/vm"
NOTES="${ASSISTANT_HOME:-$HOME/assistant}/life-agent-notes"
STAMP="$(date +%F)"

link() {  # link <target> <linkname>
    local target="$1" name="$2"
    mkdir -p "$(dirname "$name")"
    if [[ -L "$name" ]]; then
        [[ "$(readlink "$name")" == "$target" ]] && return 0
        rm "$name"
    elif [[ -e "$name" ]]; then
        mv "$name" "$name.bak-$STAMP"; echo "kept $name.bak-$STAMP"
    fi
    ln -s "$target" "$name"; echo "linked $name"
}

# Rules and skills: always loaded / loaded on demand by Claude Code.
link "$HOST_DIR/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
for s in "$HOST_DIR"/skills/*/; do link "${s%/}" "$HOME/.claude/skills/$(basename "$s")"; done
# Private map of this host (paths, services, hosts). Absent on a public-only checkout.
[[ -f "$NOTES/host/MAP.md" ]] && link "$NOTES/host/MAP.md" "$HOME/assistant/MAP.md"
# Scripts.
for f in github-mirror github-env assistant-update files-index plan-move mail-dump; do
    chmod +x "$VM/bin/$f" 2>/dev/null || true
    link "$VM/bin/$f" "$HOME/bin/$f"
done
mkdir -p "$HOME/files-index" "$HOME/github-mirror" "$HOME/paperless"
mkdir -m 700 -p "$HOME/mail" "$HOME/.config/mail-dump"
link "$VM/paperless/docker-compose.yml" "$HOME/paperless/docker-compose.yml"
link "$VM/tmux.conf" "$HOME/.tmux.conf"

[[ "$MODE" == "--system" ]] || exit 0

TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
render() { sed -e "s#@USER@#$USER#g" -e "s#@HOME@#$HOME#g" -e "s#@TZ@#$TZ_NAME#g" "$1"; }
changed=0
for u in "$VM"/systemd/*; do
    dst="/etc/systemd/system/$(basename "$u")"
    if ! render "$u" | sudo cmp -s - "$dst" 2>/dev/null; then
        render "$u" | sudo tee "$dst" >/dev/null; echo "wrote $dst"; changed=1
    fi
done
sudo install -m 755 "$VM/bin/data-mounted" /usr/local/sbin/data-mounted
# Non-interactive SSH commands do not read ~/.profile, so ~/.local/bin is not on PATH there.
[[ -x "$HOME/.local/bin/claude" ]] && sudo ln -sf "$HOME/.local/bin/claude" /usr/local/bin/claude
if [[ -d /etc/samba ]] && ! render "$VM/samba/smb.conf" | sudo cmp -s - /etc/samba/smb.conf; then
    sudo cp /etc/samba/smb.conf "/etc/samba/smb.conf.bak-$STAMP" 2>/dev/null || true
    render "$VM/samba/smb.conf" | sudo tee /etc/samba/smb.conf >/dev/null
    testparm -s >/dev/null 2>&1 && sudo systemctl reload smbd && echo "samba config updated"
fi
grep -q '^personal /data virtiofs' /etc/fstab || { echo 'personal /data virtiofs defaults,nofail 0 0' | sudo tee -a /etc/fstab; sudo mkdir -p /data; }
# mail-dump: its own Python environment (Gmail API client + HTML to text) and the search tools.
command -v rg >/dev/null && command -v jq >/dev/null || sudo apt-get install -y ripgrep jq >/dev/null
if [[ ! -x "$HOME/.venvs/mail-dump/bin/python3" ]]; then
    python3 -m venv "$HOME/.venvs/mail-dump"
    "$HOME/.venvs/mail-dump/bin/pip" install --quiet google-api-python-client google-auth-oauthlib beautifulsoup4
    echo "created ~/.venvs/mail-dump"
fi
[[ $changed -eq 1 ]] && sudo systemctl daemon-reload
for t in github-mirror assistant-update paperless-export mail-dump; do sudo systemctl enable --now "$t.timer" >/dev/null; done
systemctl list-timers --no-pager github-mirror.timer assistant-update.timer paperless-export.timer mail-dump.timer
