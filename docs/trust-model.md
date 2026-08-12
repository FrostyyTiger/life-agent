# Trust model

This document states what the agent is permitted to touch, what enforces each limit, and —
importantly — which limits are enforced by the operating system and which are merely
conventions the agent is asked to follow. Conflating those two is the most common way a
written trust model becomes fiction.

## Threat model

Three things this design takes seriously, in descending order of likelihood:

1. **The agent is wrong.** Far and away the most probable failure. It misreads a calendar
   entry, invents a deadline, marks a live thread dead, or deletes context you wrote. No
   malice required. Most of the boundaries below exist for this case.
2. **The agent is manipulated.** From v1.5 the agent reads text written by strangers. An
   email containing *"ignore your previous instructions and…"* is the standard attack on any
   mail-reading agent and should be assumed to arrive eventually. v1 has no such surface,
   which is deliberate: the boundaries get built and exercised before the attacker-controlled
   input shows up.
3. **The host is compromised by other means.** Out of scope. If someone has root on the
   machine, none of this helps.

Explicitly *not* in the threat model: the model provider behaving maliciously. Context is
sent to a frontier API by design and that trust is accepted openly — see
[`egress.md`](egress.md).

## Principals

| Principal | Unix identity | Purpose |
| --- | --- | --- |
| **You** | your own login account | Own the machine. No restrictions. |
| **Agent** | `life-agent` | Runs the brief job. Unprivileged, no `sudo`, no login shell. |
| **Publisher** | your account, via timer | Pushes the data repo to its private remote. |

The publisher is a separate principal from the agent on purpose. The agent commits — that
history is its audit trail and belongs to it — but the agent holds no GitHub credential and
has no outbound network path other than the model API. The decision about what leaves the
machine belongs to a principal that does not read untrusted input.

## Capabilities

### The agent may

| Capability | Scope | Enforced by |
| --- | --- | --- |
| Read calendar | One calendar id, read-only OAuth scope | Google OAuth scope + token file perms |
| Read config | `$LIFE_AGENT_DATA/config.yaml` | Filesystem: `0640`, owner you, group `life-agent` |
| Write threads | `$LIFE_AGENT_DATA/threads/` | Filesystem: `0770`, group `life-agent` |
| Append briefs | `$LIFE_AGENT_DATA/briefs/` | Filesystem: `0770`, plus convention (append-only) |
| Commit to data repo | `$LIFE_AGENT_DATA` local git only | No credential exists for any remote |
| Call the model API | One HTTPS endpoint | Convention + API key perms |

### The agent may not

| Prohibition | Why | Enforced by |
| --- | --- | --- |
| Gain privilege | It could break the network on a box with no physical access | **Kernel** — not in `sudo` group, no sudoers entry |
| Modify its own code | An agent that edits its constraints is not constrained | **Kernel** — `life-agent/` owned by you, `0755`, agent has r-x |
| Push to any remote | Keeps exfiltration off the agent's side of the boundary | **Kernel** — no credential readable by `life-agent` |
| Read your home directory | Blast radius | **Kernel** — your home directory is `0750` |
| Send email | v2 drafts only; sending is never delegated | Not implemented at all — the strongest enforcement there is |
| Touch system config | Network, systemd units, firewall, packages | **Kernel** — no privilege |
| Delete thread files | Wrong inferences should be cheap, not destructive | **Convention only** — see below |
| Rewrite your body text in a thread | Your notes are yours | **Convention only** — see below |
| Edit past briefs | A record you can rewrite is not a record | **Convention only** — see below |

### Honest accounting of the soft boundaries

Three of the prohibitions above are enforced by prompt instruction rather than by the
operating system, because the agent needs write access to `threads/` to do its job at all,
and POSIX permissions cannot express "may modify these fields but not those."

That means a sufficiently confused or manipulated agent *can* violate them. What the design
provides instead is **detection and recovery, not prevention**:

- Every agent run commits. A destructive edit is one `git diff` away from visible and one
  `git revert` away from undone.
- The data repo is pushed daily by a different principal, so the history survives the machine.
- `briefs/` being append-only is checked, not assumed: the brief job fails loudly if any
  pre-existing brief file has changed.

If the soft boundaries turn out to be violated in practice, the correct fix is to move
thread-writing behind a narrow tool the agent calls — one that validates field-level
permissions — rather than to write a sterner prompt. That migration is deliberately deferred
until there is evidence it is needed, and it is the most likely first change in v1.5.

## Boundary between the repositories

```
life-agent/          you: rw    agent: r-x    ← code and prompts; agent cannot write
life-agent-data/     you: rw    agent: rw     ← scoped per directory, see schema/thread.md
life-agent-notes/    you: rw    agent: ---    ← design thinking; agent has no business here
~/.config/life-agent you: rw    agent: r--    ← credentials, 0600/0640, never in any repo
```

Credentials live outside every repository because git history is permanent. A token committed
once is in that history forever, including in the copy pushed to a private remote and every
clone anyone ever makes of it.

## Approval gates

Anything irreversible or outward-facing requires a human in the loop:

| Action | Gate |
| --- | --- |
| Sending any message | Not implemented. v2 writes drafts to a directory; you send them. |
| Modifying calendar | Not implemented. Read-only scope, v1 through v2. |
| Deleting a thread | Never. `state: dropped` instead — reversible, and prevents re-proposal. |
| Confirming an inferred thread | You reply `keep`/`drop` in the brief. Proposed threads cannot nag. |
| Publishing data off-machine | Separate principal, separate timer, secret-scanned pre-push. |

## Review

This document is wrong the moment the code diverges from it. It should be re-read whenever a
capability is added, and every entry in the *enforced by* columns should be verifiable with a
command — `sudo -l -U life-agent`, `namei -l`, `git log`. If an entry cannot be checked, it is
a claim, not a control, and should be moved to the soft-boundary section above.
