---
name: builder
description: Start, check or report on development work. Any code change, new project, or new script for this host is built on the build VM, never here.
---

# Development happens on the build VM

This host does not develop. When the owner wants something built or changed in code, prepare the
task here and launch it as a lane on the build VM (host name `BUILDER_HOST` in `~/assistant/MAP.md`).

The network allows this host to open SSH to the build VM, and nothing in the other direction: work
on the build VM cannot reach this host or its data.

## Launch a lane
1. Agree the task with the owner: repo, goal, done-criteria, whether it may merge.
2. On the build VM: repo in `~/code/<repo>`, lane folder `~/lanes/<lane>/` with `kickoff.txt` (the
   full brief) and `status.md` (the lane appends one line per step and ends with `<LANE>-DONE`).
3. Start it detached (one SSH call), then check on it in a separate call:
   ```
   ssh $BUILDER_HOST 'tmux new-session -d -s <lane> -c ~/code/<repo> "claude --permission-mode auto \"$(cat ~/lanes/<lane>/kickoff.txt)\"; bash"'
   ssh $BUILDER_HOST 'tail -5 ~/lanes/<lane>/status.md; tmux capture-pane -p -J -t <lane> -S -30'
   ```
4. Never touch another lane's tmux session.

## What goes into a kickoff
- No personal data from this host: the build VM is less trusted. Describe the problem, not the files.
- Scripts meant for this host: develop in `life-agent/host/`, open a PR; the owner merges; this host
  pulls it nightly (`assistant-update`).
