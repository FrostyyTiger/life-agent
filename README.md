# life-agent

A self-hosted personal agent that reads your calendar and mail, keeps a version-controlled
record of your ongoing commitments, and writes you one short brief each morning.

It is deliberately small. It does not send anything, it does not act on your behalf, and it
cannot modify its own code. What it does is notice — that the appointment on Thursday needed
you to start on Monday, that nobody has replied about the thing you're blocked on, that a
project you care about has been silent for three weeks, that four new mails this morning
actually need you and forty don't.

## Why this exists

Calendars tell you a thing is happening. They don't tell you a thing needed *runway*. Task
apps assume you will reliably write tasks down, which is precisely the capability the people
who need them most are short of. This system splits the difference: the agent proposes what
it thinks your open threads are, and you confirm or dismiss them in one word from the brief
you were already reading.

The other half of the project is the part usually left implicit: a written account of what an
agent with access to your life is permitted to touch, what leaves your machine, and how you
find out when it breaks. Those documents are in [`docs/`](docs/) and they are the point as
much as the code is.

## What it is not

Not a product, not a framework, not a platform. There is no install wizard and no plugin API.
It is a reference implementation of an architecture plus the conventions that make it safe to
run, small enough that one person can read all of it in an afternoon.

## Architecture

Three repositories and two unversioned directories, deliberately separated:

| Location | Visibility | Contents | Agent access |
| --- | --- | --- | --- |
| `life-agent/` (this repo) | public | code, prompts, schema, docs | **read + execute only** |
| `life-agent-data/` | private | `threads/`, `briefs/`, `config.yaml`, `mail-feedback.jsonl` | read + write, scoped |
| `~/.config/life-agent/` | not in git | OAuth tokens, API key | read only (calendar); mail's three tokens are **owned by the agent**, not merely readable — see below |
| `$LIFE_AGENT_STATE` | not in git, not even the owner's | `mail.db`, embeddings, `claude -p`'s scratch dirs | read + write; **the owner cannot read this without `sudo`** |
| `life-agent-notes/` | private | design thinking, decision log | none |

The agent runs as a dedicated unprivileged user with no `sudo` and no write access to this
repository. An agent that can edit the code constraining it is not constrained. See
[`docs/trust-model.md`](docs/trust-model.md).

Mail (`src/mail/`, [`docs/plans/mail-v1.md`](docs/plans/mail-v1.md)) tightens this further:
the archive and its credentials belong to the agent alone, not merely to a scope the owner
happens not to use. The owner's side — including every other Claude Code session on the
host — reaches it only through a capped, read-only query socket. See
`docs/trust-model.md`'s "Mail: prompt injection" section for how the same agent stays safe
once it's reading text written by strangers, which mail is the first thing in this project
to do.

This repo contains no path to any real installation. The code takes `LIFE_AGENT_DATA` from the
environment and refuses to start if it is unset — there is no default and no fallback. A fresh
clone runs against [`examples/threads/`](examples/threads/), which is a synthetic life in the
real format.

## The data model

One markdown file per thread, YAML frontmatter, human-editable. Roughly six fields, which
between them cover four distinct ways things go wrong:

| What goes wrong | Which fields catch it |
| --- | --- |
| Events sneak up on you | `due` + `lead` — surfaces at `due - lead`, not at `due` |
| Deadlines creep up | same |
| You forget to follow up | `last_touched` + `patience` |
| Long projects go quiet | same |

Full specification: [`schema/thread.md`](schema/thread.md).

## Status

🚧 **Partially implemented, out of the original order.** The calendar half of v1 (thread
store, calendar brief, dead-man's switch) is still a design, not code — `src/README.md`'s
module table for it is accurate as written. What's actually built and tested is
**mail-v1** (`src/mail/`): a searchable archive, tagging, and a morning digest, originally
scoped as half of the v1.5 milestone below, brought forward instead. It's code-complete,
tested (`uv run pytest` — see `docs/status/mail-v1.md` for the current count), and one
`sudo ./setup/bootstrap.sh --apply` plus a handful of owner-side credentials away from
running for real; `docs/status/mail-v1.md` has the stage-by-stage record and the exact
NEED-MARCEL list blocking that last step.

Roadmap:

- **v1** — calendar read, thread store, morning brief, dead-man's switch. Design complete,
  not started.
- **mail-v1** (was v1.5's email half) — searchable mail archive, tagging, and a morning
  digest. Code complete; blocked on owner credentials and a `sudo` bootstrap run. See
  [`docs/plans/mail-v1.md`](docs/plans/mail-v1.md) and
  [`docs/status/mail-v1.md`](docs/status/mail-v1.md).
- **v1.5's other half** — automatic thread detection from mail + calendar activity. Not
  started; needs v1's thread store to exist first.
- **v2** — drafted replies, reviewed by you, never sent by the agent.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Components, data flow, what runs when (calendar v1) |
| [`docs/trust-model.md`](docs/trust-model.md) | Principals, capability boundaries, what enforces each — includes mail-v1's prompt-injection accounting |
| [`docs/egress.md`](docs/egress.md) | Every byte that leaves the machine, and why |
| [`docs/failure-modes.md`](docs/failure-modes.md) | What breaks, how it is detected, what happens next |
| [`schema/thread.md`](schema/thread.md) | The thread file format |
| [`docs/plans/mail-v1.md`](docs/plans/mail-v1.md) | The mail archive/digest design, stage by stage |
| [`docs/status/mail-v1.md`](docs/status/mail-v1.md) | What's built, how it was verified, the owner's runbook |

## License

[MIT](LICENSE)
