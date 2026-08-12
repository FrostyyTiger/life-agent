# setup/

Everything needed to install the boundaries described in
[`../docs/trust-model.md`](../docs/trust-model.md).

| File | Runs as | Purpose |
| --- | --- | --- |
| `bootstrap.sh` | you, with `sudo` | Creates the agent user, sets ownership and modes. Dry-run by default. |
| `systemd/life-agent-brief.*` | `life-agent` | The 07:00 daily brief job |
| `systemd/life-agent-publish.*` | you | 07:30 secret-scan and push |
| `systemd/life-agent-deadman.*` | you | 08:00 dead-man's switch |
| `deadman.sh` | you | Asserts today's brief exists; notifies only on absence |
| `publish.sh` | you | Visibility check, secret scan, push |

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
      "$f" > "/tmp/life-agent-units/$(basename "$f")"
done
grep -r '__' /tmp/life-agent-units && echo "unsubstituted placeholders remain" || true

sudo cp /tmp/life-agent-units/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now life-agent-brief.timer \
                            life-agent-publish.timer \
                            life-agent-deadman.timer
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

## Verify the boundaries, do not assume them

Every claim in the trust model should be checkable with a command. After installing:

```bash
sudo -l -U life-agent                              # expect: not allowed to run sudo
sudo -u life-agent touch ~/life-agent/x            # expect: Permission denied
sudo -u life-agent touch ~/life-agent-data/threads/x  # expect: success
sudo -u life-agent cat ~/.config/life-agent/api-key   # expect: success (ACL)
sudo -u life-agent ls ~/                           # expect: Permission denied
systemctl list-timers 'life-agent-*'               # expect: three timers
```

If one of these does not behave as stated, the trust model is wrong and should be corrected
rather than the result explained away.

## What this deliberately does not do

`bootstrap.sh` touches no networking, no firewall rules, no packages, and no pre-existing
system unit. The host this was written for is headless, at a remote location, with no physical
access — a setup script that reconfigures the network there is a setup script that can end the
machine. Any future addition to this directory should hold to the same line.
