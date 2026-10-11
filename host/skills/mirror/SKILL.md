---
name: mirror
description: Read the owner's GitHub repositories from the local mirror, check the mirror's health, or restore a repo from it.
---

# Repo mirror

`~/github-mirror/<repo>.git`: bare mirror clones of every repository owned by the owner's GitHub
account (organisation repos excluded), refreshed nightly by `github-mirror.timer`.

- Health: `cat ~/github-mirror/last-run.txt` (one line: time, OK/FAIL, counts); details in `mirror.log`.
  `NO-TOKEN` or `API-FAIL` usually means the token expired: ask the owner for a new one (they type it,
  see `~/assistant/MAP.md` for the command).
- Read a file without a checkout: `git -C ~/github-mirror/<repo>.git show HEAD:<path>`;
  history: `git -C ... log --oneline -20`. For more, clone into `~/work/<repo>` (read only; work
  happens on the build VM).
- Exclude a repo: add its name to `~/github-mirror/exclude.txt`.
- Restore a lost or damaged GitHub repo: `git clone --mirror` from here to a new remote, or push
  `--mirror` back. That is an outward action: ask first.
- The mirror never prunes refs and never deletes a local mirror, so deleted branches and repos
  survive here. Git LFS objects are not mirrored.
- The token is fine-grained and read-only; it cannot push.
