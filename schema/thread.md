# Thread file format

A *thread* is one ongoing thing in your life: a commitment, a deadline, a conversation you
owe someone, a project that can go quiet. One thread, one file, plain markdown with YAML
frontmatter, stored in `$LIFE_AGENT_DATA/threads/`.

The format is designed so that you can fix any mistake the agent makes with a text editor,
and so that `git log` tells you what the agent believed about your life on any given day.

## Example

```markdown
---
id: roof-quote-follow-up
title: Quote for the roof gutter work
state: waiting
due: 2026-09-05
lead: 10d
last_touched: 2026-08-01
patience: 10d
waiting_on: Hartmann Dachbau (quotes@example.com)
source: manual
confidence: confirmed
tags: [house]
---

They came out on 1 August and said a written quote would follow "early next week". Nothing
since. If it has not arrived by the 11th, chase — the scaffolding has to be booked before
the work can be scheduled, and that is another two weeks on its own.
```

## Fields

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `id` | yes | slug | Stable identifier. Must match the filename without `.md`. Never reused. |
| `title` | yes | string | One line, human phrasing. This is what appears in the brief. |
| `state` | yes | enum | `active` · `waiting` · `done` · `dropped` — see below. |
| `due` | no | ISO date | A real, externally imposed date. Omit if there isn't one. |
| `lead` | no | duration | How much runway you need *before* `due`. Meaningless without `due`. |
| `last_touched` | yes | ISO date | Last time anything actually happened on this thread. |
| `patience` | yes | duration | How long silence is acceptable before it resurfaces. |
| `waiting_on` | no | string | Who owes the next move. Only meaningful when `state: waiting`. |
| `source` | yes | enum | `manual` · `calendar` · `inferred` — where this thread came from. |
| `confidence` | yes | enum | `proposed` · `confirmed` — see the capture loop below. |
| `tags` | no | list | Free-form. Used only for grouping in the brief. |

Durations are a plain integer plus a unit: `3d`, `2w`, `6m`. No compound forms.

The body below the frontmatter is unstructured. Write whatever context future-you needs.
The agent reads it for the brief but never rewrites it — see *Ownership* below.

## States

| State | Meaning | Appears in brief? |
| --- | --- | --- |
| `active` | You owe the next move. | Yes, on staleness or lead time. |
| `waiting` | Someone else owes the next move. | Yes, on staleness — that's the chase reminder. |
| `done` | Finished. Kept for history. | No. |
| `dropped` | Consciously abandoned. Kept so it is not re-proposed. | No. |

`dropped` exists specifically so that a thread you dismissed does not get re-inferred next
week. Deleting the file instead would lose that, and the agent would helpfully propose it
again forever.

## The two alarms

Everything the brief surfaces comes from exactly two comparisons against today's date.

**Lead-time alarm** — for dated threads:

```
due is set  AND  today >= (due - lead)  →  surface it
```

This is the one a calendar cannot do for you. The passport that expires on 5 September needs
to appear in your brief in early July, not on 4 September. `lead` is the whole point of the
system; it is worth thinking about honestly for each thread rather than defaulting it.

**Staleness alarm** — for anything that can go quiet:

```
today >= (last_touched + patience)  →  surface it
```

`patience` is per-thread, never global. A supplier who said "a few days" has a patience of
about a week. A long-running personal project might be a month. Global staleness rules
generate noise; per-thread patience generates signal.

A thread may trip both alarms. It appears once.

## The capture loop

The problem this system exists to solve is that reliably writing things down is hard. So the
agent proposes, and you confirm — from the brief, in one word.

1. The agent notices something that looks like an open thread (a calendar event with no
   matching thread, a repo untouched for weeks, later an unanswered email).
2. It writes a thread file with `confidence: proposed` and `source: inferred`.
3. Tomorrow's brief lists it under **Proposed** with a one-line justification.
4. You reply `keep` or `drop`. `keep` sets `confidence: confirmed`; `drop` sets
   `state: dropped`.

Proposed threads never trip alarms. They cannot nag you until you have agreed they are real.
This keeps a wrong inference cheap: it costs you one line of a brief, once.

## Ownership

Within `$LIFE_AGENT_DATA`, three different ownership rules apply, and they are enforced by
filesystem permissions rather than good intentions (see `docs/trust-model.md`):

| Path | Agent | You |
| --- | --- | --- |
| `threads/` | read + write | read + write |
| `briefs/` | append only | read only |
| `config.yaml` | read only | read + write |

`threads/` is genuinely shared — that is the collaboration surface. `briefs/` is a record, and
a record you can rewrite is not a record. `config.yaml` is yours; the agent may consult your
defaults but never adjust them.

When the agent revises a thread it must preserve the body text. It may update `state`,
`last_touched`, `confidence`, and append a dated line to the body. It may not rewrite or
delete what you wrote. This rule is convention enforced by prompt, not by the kernel, and it
is listed as such in the trust model.

## Filenames

`threads/<id>.md`, lowercase, hyphen-separated, no dates in the name. Threads outlive their
start dates and a filename with a date in it becomes a lie.
