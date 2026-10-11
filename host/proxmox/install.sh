#!/usr/bin/env bash
# Install the archive-disk scripts and timers on the Proxmox host (as root). Needs /etc/default/personal
# (from personal.env.example). Does not create the pool: see host/RESTORE.md for that.
set -euo pipefail
D="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
[[ -f /etc/default/personal ]] || { echo "create /etc/default/personal first (personal.env.example)"; exit 1; }
for f in "$D"/bin/*; do install -m 755 "$f" "/usr/local/sbin/$(basename "$f")"; done
for u in "$D"/systemd/*; do install -m 644 "$u" "/etc/systemd/system/$(basename "$u")"; done
systemctl daemon-reload
systemctl enable personal-up.service
for t in personal-snap-hourly personal-snap-daily personal-snap-monthly personal-scrub pbs-host-backup; do
    systemctl enable --now "$t.timer"
done
