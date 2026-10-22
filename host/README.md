# host/: the personal host, as code

Everything needed to rebuild the assistant's home described in
[`docs/personal-host.md`](../docs/personal-host.md), with nothing personal in it. Host-specific
details (names, addresses, the map of what lives where) are in the private `life-agent-notes/host/`.

| Path | Installed to | What |
| --- | --- | --- |
| `CLAUDE.md` | `~/.claude/CLAUDE.md` (link) | the assistant's ground rules, always loaded |
| `claude/settings.json` | `~/.claude/settings.json` (link) | the `mail-link-guard` hook: a link found in mail opens only after the owner's click |
| `skills/<name>/SKILL.md` | `~/.claude/skills/<name>` (link) | how-to per module, loaded only when a task needs it |
| `vm/bin/` | `~/bin/` (links) | `github-mirror`, `assistant-update`, `files-index`, `plan-move`, `github-env`, `mail-dump`, `mail-link-guard` |
| `vm/systemd/` | `/etc/systemd/system/` (rendered) | mirror 00:30, assistant update 00:45, document export 01:00, mail copy every 15 min; `assistant-remote` (off until the owner enables it) |
| `vm/samba/smb.conf` | `/etc/samba/smb.conf` (rendered) | tailnet-only shares `data` and `scan` |
| `vm/paperless/` | `~/paperless/docker-compose.yml` (link) + env examples | the document server |
| `vm/tmux.conf` | `~/.tmux.conf` (link) | the persistent assistant session |
| `vm/install.sh` | | `--user` (links, no sudo) or `--system` (also units, Samba, fstab) |
| `proxmox/` | `/usr/local/sbin`, `/etc/systemd/system` on the hypervisor | archive pool up/down, snapshots, scrub, host-level backup |

## How changes arrive

Nothing is edited on the host itself. A change is made on the build VM, opened as a PR here, and
merged by the owner; `assistant-update` fast-forwards the clones every night and re-runs
`install.sh --user`. Because rules and skills are links into the clone, a merged change is live the
next morning. Changes to `vm/systemd/` or `vm/samba/` also need one `install.sh --system`.

## Remote Control (phone and browser)

`assistant-remote.service` keeps `claude remote-control` running in a tmux session `remote`, so new
sessions on this host can be started from the Claude app or claude.ai/code (environment `assistant`).
Each one runs here, with these rules and skills. Anyone who can sign in to the owner's Claude account
can start sessions here: the account needs 2FA.

First time, by hand, so the trust and enable prompts are answered and saved:
`ssh -t <host> 'cd ~/assistant && claude remote-control --name assistant'` → answer `y` twice →
Ctrl+C. Then `sudo systemctl enable --now assistant-remote`. Stop: `sudo systemctl stop assistant-remote`.

## Keeping CLAUDE.md small

`CLAUDE.md` is in every session's context, so it holds only what must always apply: the scope, what
needs the owner's yes, what never happens, privacy, where to look. Anything procedural goes into a
skill: Claude Code shows only the skill's one-line description until the task needs it. A new module
(browser) adds a skill, not lines to `CLAUDE.md`.

Restoring: [`RESTORE.md`](RESTORE.md).
