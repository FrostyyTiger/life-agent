# setup/

Everything needed to install the boundaries described in
[`../docs/trust-model.md`](../docs/trust-model.md).

| File | Runs as | Purpose |
| --- | --- | --- |
| `bootstrap.sh` | you, with `sudo` | Creates the agent user, sets ownership and modes. Dry-run by default. |
| `systemd/life-agent-brief.*` | `life-agent` | The 07:00 daily brief job |
| `systemd/life-agent-publish.*` | you | 07:30 secret-scan and push |
| `systemd/life-agent-deadman.*` | you | 08:00 dead-man's switch |
| `deadman.sh` | you | Asserts today's mail digest exists; notifies only on absence |
| `publish.sh` | you | Visibility check, secret scan, push |
| `systemd/life-agent-mail-sync.*` | `life-agent` | Fetch new/changed mail every 15 min ([mail-v1](../docs/plans/mail-v1.md)) |
| `systemd/life-agent-mail-tag.*` | `life-agent` | Tag new mail + process digest-reply feedback, offset 5 min after sync |
| `systemd/life-agent-mail-digest.*` | `life-agent` | Compose + insert the 06:30 mail digest |
| `systemd/life-agent-mail-query.service` | `life-agent` | Always-on read-only query socket (`/run/life-agent/mail.sock`) |

## Order

```bash
./setup/bootstrap.sh                # dry run — read what it intends to do
./setup/bootstrap.sh --apply        # then, if you agree
```

The unit files are templates. This repository contains no path to any real installation, so
substitute yours before installing:

```bash
mkdir -p /tmp/life-agent-units
for f in setup/systemd/*; do
  sed -e "s#__OWNER__#$(id -un)#g" \
      -e "s#__CODE_DIR__#$HOME/life-agent#g" \
      -e "s#__DATA_DIR__#$HOME/life-agent-data#g" \
      -e "s#__CONF_DIR__#$HOME/.config/life-agent#g" \
      -e "s#__STATE_DIR__#$HOME/life-agent-state#g" \
      -e "s#__CLAUDE_BIN_DIR__#$(dirname "$(command -v claude)")#g" \
      "$f" > "/tmp/life-agent-units/$(basename "$f")"
done
grep -r '__' /tmp/life-agent-units && echo "unsubstituted placeholders remain" || true

sudo cp /tmp/life-agent-units/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now life-agent-brief.timer \
                            life-agent-publish.timer \
                            life-agent-deadman.timer
sudo systemctl enable --now life-agent-mail-sync.timer \
                            life-agent-mail-tag.timer \
                            life-agent-mail-digest.timer \
                            life-agent-mail-query.service
```

Credentials are placed by hand — the script never writes one. Put `google-oauth.json` and
`api-key` in `~/.config/life-agent/` at mode `0600`, then grant the agent read access with a
targeted ACL rather than by loosening the mode:

```bash
setfacl -m u:life-agent:x  ~/.config/life-agent
setfacl -m u:life-agent:r  ~/.config/life-agent/{google-oauth.json,api-key}
```

For the dead-man's switch to reach your phone, put a random topic string in
`~/.config/life-agent/ntfy-topic` and subscribe to it in the ntfy app. Absent that file the
switch still logs to the journal and exits non-zero; it simply cannot reach you.

**Mail-v1's credentials are different**: `google-client.json` stays owned by you (place it
yourself, per [`docs/plans/mail-v1.md`](../docs/plans/mail-v1.md)'s owner items), but
`gmail-readonly-token.json`, `gmail-insert-token.json`, and `claude-oauth-token` — created by
`mail auth readonly`, `mail auth insert`, and `claude setup-token` respectively — get
**chowned to `life-agent`** by `bootstrap.sh --apply`, not ACL'd. That is the point: unlike
the calendar system's credentials (which the agent merely reads via ACL, still owned by you),
the mail tokens belong to `life-agent` alone, and you lose direct read access to them the
moment bootstrap runs. If you need to inspect one, `sudo -u life-agent cat …` — a deliberate,
visible act, not something any of your Claude Code sessions can do by accident.

## Verify the boundaries, do not assume them

Every claim in the trust model should be checkable with a command. After installing:

```bash
sudo -l -U life-agent                              # expect: not allowed to run sudo
sudo -u life-agent touch ~/life-agent/x            # expect: Permission denied
sudo -u life-agent touch ~/life-agent-data/threads/x  # expect: success
sudo -u life-agent cat ~/.config/life-agent/api-key   # expect: success (ACL)
sudo -u life-agent ls ~/                           # expect: Permission denied
systemctl list-timers 'life-agent-*'               # expect: three timers (+ three more, mail-v1)

# mail-v1 specific — the whole point is that these fail for you:
cat ~/life-agent-state/mail.db                          # expect: Permission denied
cat ~/.config/life-agent/gmail-readonly-token.json       # expect: Permission denied
sudo -u life-agent touch ~/life-agent/x                  # expect: Permission denied (same file, same rule)
sudo -u life-agent cat ~/life-agent-state/mail.db        # expect: success — this is the sanctioned path in
systemctl is-active life-agent-mail-query                # expect: active
mail status                                              # expect: answers via the query socket, not a direct read
```

If one of these does not behave as stated, the trust model is wrong and should be corrected
rather than the result explained away.

## What this deliberately does not do

`bootstrap.sh` touches no networking, no firewall rules, no packages, and no pre-existing
system unit. The host this was written for is headless, at a remote location, with no physical
access — a setup script that reconfigures the network there is a setup script that can end the
machine. Any future addition to this directory should hold to the same line.
