# life-agent

A self-hosted personal agent that reads your calendar, keeps a version-controlled record of
your ongoing commitments, and writes you one short brief each morning.

It is deliberately small. It does not send anything, it does not act on your behalf, and it
cannot modify its own code. What it does is notice — that the appointment on Thursday needed
you to start on Monday, that nobody has replied about the thing you're blocked on, that a
project you care about has been silent for three weeks.

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

Three repositories and one unversioned directory, deliberately separated:

| Location | Visibility | Contents | Agent access |
| --- | --- | --- | --- |
| `life-agent/` (this repo) | public | code, prompts, schema, docs | **read + execute only** |
| `life-agent-data/` | private | `threads/`, `briefs/`, `config.yaml` | read + write, scoped |
| `~/.config/life-agent/` | not in git | OAuth tokens, API key | read only |
| `life-agent-notes/` | private | design thinking, decision log | none |

The agent runs as a dedicated unprivileged user with no `sudo` and no write access to this
repository. An agent that can edit the code constraining it is not constrained. See
[`docs/trust-model.md`](docs/trust-model.md).

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

🚧 **Design complete, implementation not started.** The schema, trust model, egress accounting,
and failure-mode analysis are written. `src/` is currently a description of the modules rather
than the modules themselves. Treat this repo as a design document with a directory structure.

Roadmap:

- **v1** — calendar read, thread store, morning brief, dead-man's switch. No email, no drafts.
- **v1.5** — email ingestion and automatic thread detection. This is where the prompt-injection
  analysis has to be written before a line of code is.
- **v2** — drafted replies, reviewed by you, never sent by the agent.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | Components, data flow, what runs when |
| [`docs/trust-model.md`](docs/trust-model.md) | Principals, capability boundaries, what enforces each |
| [`docs/egress.md`](docs/egress.md) | Every byte that leaves the machine, and why |
| [`docs/failure-modes.md`](docs/failure-modes.md) | What breaks, how it is detected, what happens next |
| [`schema/thread.md`](schema/thread.md) | The thread file format |

## License

[MIT](LICENSE)
