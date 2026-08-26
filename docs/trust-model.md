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
2. **The agent is manipulated.** Mail-v1 (`docs/plans/mail-v1.md`) is the first surface
   where the agent reads text written by strangers. An email containing *"ignore your
   previous instructions and…"* is the standard attack on any mail-reading agent and is
   assumed to arrive eventually — `examples/mail/008-prompt-injection.eml` exists
   specifically to keep this case exercised in CI, not left hypothetical. See
   ["Mail: prompt injection"](#mail-prompt-injection) below for what actually bounds it.
3. **The host is compromised by other means.** Out of scope. If someone has root on the
   machine, none of this helps.

Explicitly *not* in the threat model: the model provider behaving maliciously. Context is
sent to a frontier API by design and that trust is accepted openly — see
[`egress.md`](egress.md).

## Principals

| Principal | Unix identity | Purpose |
| --- | --- | --- |
| **You** | your own login account | Own the machine. No restrictions. |
| **Agent** | `life-agent` | Runs the brief job **and** every mail-v1 job (sync, tag, digest, the query socket). Unprivileged, no `sudo`, no login shell, has a home directory only because `claude -p` and the HF cache need one. |
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
| Read mail (readonly) | One Gmail account, `gmail.readonly` OAuth scope | Google OAuth scope + token file, `0600`, owned by `life-agent` |
| Insert one mail into the owner's own inbox | `gmail.insert` scope — this scope *cannot send*; recipient is a constant in `digest.py`, never model output | Google OAuth scope + token file, `0600`, owned by `life-agent` |
| Call `claude -p` with no tools | A long-lived subscription token (`claude setup-token`); tagging (haiku) and digest composition (sonnet) only | Convention (`--bare --tools ""`) + token file, `0600`, owned by `life-agent` |
| Answer archive queries over a socket | `/run/life-agent/mail.sock` — `/status`, `/search`, `/show`, all read-only, no endpoint can list the whole archive, `limit` capped at 50 | Convention (`serve.py`'s fixed endpoint set) — see the honest note below |

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
| Send mail, or write to any mailbox but your own | `gmail.insert` cannot send; the recipient of the one message it does write is a constant, never model output | **Kernel** — the OAuth scope itself has no send capability |
| Read the mail archive or any mail token, if you are the owner (and therefore any other Claude Code session on this host) | Isolation is the entire point of mail-v1 — see "Why this shape" in `docs/plans/mail-v1.md` | **Kernel** — `$LIFE_AGENT_STATE` `0700` owned by `life-agent`, no group bits; the three mail tokens `0600` owned by `life-agent`. Verify: `namei -l $LIFE_AGENT_STATE/mail.db` shows no access for you past the state directory, and `cat $LIFE_AGENT_STATE/mail.db` / `cat $LIFE_AGENT_CONF/gmail-readonly-token.json` both fail as you |

**An honest note on that last row's boundary**: the query socket at
`/run/life-agent/mail.sock` (mode `0660`, group `life-agent`) *is* reachable by any
process running as you — that is the intended interface, not a leak. What it returns is
capped search/show results through `serve.py`'s three fixed endpoints, never the
database file itself, never a token, and never an unbounded dump (no endpoint can list
the whole archive). Calling it is an explicit, visible act in a session transcript;
opening `mail.db` directly is not possible without `sudo`, which is the deliberate line.

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

## Mail: prompt injection

Mail-v1's tagging and digest models are the first place this project puts a model in
contact with text an attacker chose, not text the owner wrote. In descending order of how
much weight each control actually carries:

- **No tools, ever.** Every mail-path model call runs as `claude -p --bare --tools ""`. It
  cannot call a tool, browse, or write a file, no matter what the mail says. This is the
  single strongest control here and the reason everything below is defense in depth
  rather than the whole story.
- **Schema-validated output, checked twice.** Every reply is checked against a fixed shape
  (`category` enum, `importance` 0-3, capped string lengths, an ISO-or-null `deadline`)
  by `tag.py`/`digest.py` themselves, independently of whatever `--json-schema` enforces
  on the model-provider side. A model that complies with an injected instruction can
  still only produce a valid tag or a valid digest line — no field an attacker's text
  can steer exists outside that shape.
- **Untrusted content is labelled and boundary-sanitized.** `prompts/tag.md` tells the
  model every `<mail id="…">` block is data, not instructions. The literal substrings a
  model would read as block syntax (`<mail`, `</mail>`, and the code's own `[truncated]`
  marker) are neutralized in subject/from/body before rendering, so a message cannot
  forge the end of its own block and open a fake one impersonating a different id.
- **The worst case is a wrong tag or a misleading one-line summary.** Not a sent email,
  not a deleted message, not a credential, not a tool call — none of those are reachable
  from this path regardless of what the model decides to do with an injected instruction.
- **Feedback is scoped to a conversation the archive itself started.** `mail feedback`
  only parses replies whose `In-Reply-To`/`References` names a `Message-ID` this archive
  generated and sent to the owner's own address. A stranger's mail cannot inject a
  feedback command by forging those headers into looking like a reply to something that
  was never sent.
- **v2 drafting (not built) inherits the existing send gate above**: it will require an
  explicit owner instruction to turn on, and will only ever write a draft to a directory
  for the owner to send — never an autonomous send.

This section exists because the README promised it the moment email ingestion was still
a future milestone. That promise is why these boundaries were built and exercised
(`examples/mail/008-prompt-injection.eml`, the block-boundary-forging tests in
`tests/mail/test_tag.py`) before any attacker-controlled input actually arrived, not after.

## Boundary between the repositories

```
life-agent/          you: rw    agent: r-x    ← code and prompts; agent cannot write
life-agent-data/     you: rw    agent: rw     ← scoped per directory, see schema/thread.md
life-agent-notes/    you: rw    agent: ---    ← design thinking; agent has no business here
~/.config/life-agent you: rw    agent: r--    ← google-client.json: owner-owned, agent
                                                 reads it via ACL, never in any repo
                     you: ---   agent: rw     ← the three mail tokens: owned by
                                                 life-agent once bootstrap chowns them —
                                                 you lose direct read access to these
$LIFE_AGENT_STATE     you: ---   agent: rw     ← not a repo at all: mail.db + HF cache,
                                                 0700, no group bits — you reach it only
                                                 via the query socket (see Capabilities)
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
