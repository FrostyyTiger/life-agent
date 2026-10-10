# setup/

Everything needed to install the boundaries described in
[`../docs/trust-model.md`](../docs/trust-model.md).

| File | Runs as | Purpose |
| --- | --- | --- |
| `bootstrap.sh` | you, with `sudo` | Creates the agent user, sets ownership and modes. Dry-run by default. |
| `systemd/life-agent-brief.*` | `life-agent` | The 07:00 daily brief job — **not installed** (below): its job, `src/main.py`, doesn't exist yet |
| `systemd/life-agent-publish.*` | you | 07:30 secret-scan and push |
| `systemd/life-agent-deadman.*` | you | 08:00 dead-man's switch |
| `deadman.sh` | you | Asserts today's mail digest exists; notifies only on absence |
| `publish.sh` | you | Visibility check, secret scan, push |
| `systemd/life-agent-mail-sync.*` | `life-agent` | Fetch new/changed mail every 15 min and keep each raw message ([mail-v1](../docs/plans/mail-v1.md), [mail-personal-v1](../docs/plans/mail-personal-v1.md)); runs `--no-embed` |
| `systemd/life-agent-mail-embed.*` | `life-agent` | Embed un-embedded mail hourly, 45-min budget, idle priority, `MemoryMax=4G` |
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
# life-agent-brief.timer is deliberately NOT enabled here — see the file table above.
sudo systemctl enable --now life-agent-publish.timer \
                            life-agent-deadman.timer
sudo systemctl enable --now life-agent-mail-sync.timer \
                            life-agent-mail-tag.timer \
                            life-agent-mail-digest.timer \
                            life-agent-mail-query.service
```

### Archive only (mail-personal-v1)

The archive install from [`docs/plans/mail-personal-v1.md`](../docs/plans/mail-personal-v1.md):
a copy of every mail (`mail.db` + `raw/<YYYY>/<MM>/<id>.eml.gz`), embeddings next to it, and
search through the socket. Three units, nothing else: **sync, embed, query**. Tagging, the
digest, `gmail.insert`, `claude -p` and the publish/brief/deadman timers stay off; their
templates are simply not installed, and no Claude token or insert token is minted.

```bash
# 1. dependencies: the normal torch wheels (CUDA on a GPU host; on a host without a GPU
#    the same install runs embedding on the CPU fallback)
uv sync
# 2. bootstrap with the archive's own state dir; dry run first, then --apply.
#    Warnings about a missing Claude token or insert token are expected.
STATE_DIR=/var/lib/life-agent/state ./setup/bootstrap.sh
STATE_DIR=/var/lib/life-agent/state ./setup/bootstrap.sh --apply
# 3. the model, once, as life-agent (cached under $STATE_DIR/models)
sudo -u life-agent env LIFE_AGENT_DATA=… LIFE_AGENT_STATE=/var/lib/life-agent/state \
  LIFE_AGENT_CONF=… .venv/bin/python -c \
  'from pathlib import Path; from src.mail.embed import SentenceTransformerEmbedder as E; E(Path("/var/lib/life-agent/state/models"))'
# 4. template only the three archive units (set the four dirs for your install)
CODE_DIR=…; DATA_DIR=…; CONF_DIR=…; STATE_DIR=/var/lib/life-agent/state
mkdir -p /tmp/life-agent-units
for f in setup/systemd/life-agent-mail-{sync.service,sync.timer,embed.service,embed.timer,query.service}; do
  sed -e "s#__CODE_DIR__#$CODE_DIR#g" -e "s#__DATA_DIR__#$DATA_DIR#g" \
      -e "s#__CONF_DIR__#$CONF_DIR#g" -e "s#__STATE_DIR__#$STATE_DIR#g" \
      -e "s#__OWNER__#$(id -un)#g" \
      -e "s#:__CLAUDE_BIN_DIR__##g" -e "s#__CLAUDE_BIN_DIR__#(unused)#g" \
      "$f" > "/tmp/life-agent-units/$(basename "$f")"
done
grep -r '__' /tmp/life-agent-units && echo "unsubstituted placeholders remain" || true
sudo cp /tmp/life-agent-units/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now life-agent-mail-sync.timer \
                            life-agent-mail-embed.timer \
                            life-agent-mail-query.service
```

`__CLAUDE_BIN_DIR__` appears only in the sync unit's `PATH`; the archive has no use for
`claude`, so the loop drops it instead of pointing it anywhere. Then, as `life-agent`:

```bash
mail sync --budget 60 --no-embed   # smoke test
mail embed --measure 50            # messages/min, backlog hours, peak RSS and VRAM; commits nothing
mail status                        # gmail total, rows, raw files, not embedded, disk
mail verify                        # after the backfill: exit 0 proves every mail is copied
mail backfill-raw                  # only if verify lists rows without a raw file
```

`systemctl list-timers 'life-agent-*'` should show exactly two timers (mail-sync, mail-embed).

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
systemctl list-timers 'life-agent-*'               # expect: publish + deadman + three mail timers (five)

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
