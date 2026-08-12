# Data egress

Every path by which data leaves the machine, what travels it, and why that trade was accepted.

This project describes itself as self-hosted. Self-hosted here means **you own the storage,
the schedule, the memory, and the policy** — not that reasoning happens locally. Inference
runs against a frontier model API. Pretending otherwise would make every other claim in this
repository less believable, so the accounting is stated plainly.

## The ledger

| # | What leaves | Destination | When | Trigger | Contains |
| --- | --- | --- | --- | --- | --- |
| 1 | Thread titles, bodies, dates; today's calendar entries; the brief prompt | Anthropic API | Daily, ~07:00 | Scheduled brief job | **Yes — personal.** Names, commitments, appointments, whatever you wrote in thread bodies. |
| 2 | Full contents of `threads/` and `briefs/` | GitHub (private repo) | Daily, after the brief | Publisher timer | **Yes — personal.** The complete record. |
| 3 | Calendar API request | Google | Daily, ~07:00 | Scheduled brief job | Read-only query against one calendar id. Google already has this data. |
| 4 | A fixed string: *"No brief for YYYY-MM-DD"* | ntfy.sh | Only on failure | Dead-man's switch at 08:00 | **No personal data.** A date and the fact that a file is missing. |

Nothing else leaves. Specifically: no telemetry, no analytics, no crash reporting, no
third-party dependencies that phone home, and the agent holds no credential for any
destination other than row 1's endpoint.

## Row 1 — the model API

**What this costs you:** the substance of your commitments and appointments transits to, and
is processed by, a third party. This is not mitigated by the rest of the design. It is the
central trade the project makes.

**Why it is accepted:** the value of the whole system depends on judgement quality — deciding
what is genuinely worth surfacing, phrasing a brief so it gets read, inferring that two
calendar entries and a stalled repo are one thread. Local inference on the available hardware
(8 GB VRAM, so a heavily quantised model in the ~12B class) is not close to sufficient for
that, and a system that surfaces the wrong three things every morning is worse than no system.

**What is done about it anyway:**

- Only threads that trip an alarm are sent, not the whole store. Most days that is a handful
  of files, not your entire life.
- Thread bodies are yours to write. Anything you would not send, do not put in a thread body.
  This is a real constraint on the user, and stating it is more honest than implying the
  system protects you from it.
- `config.yaml` supports an `exclude_tags` list. Threads carrying an excluded tag are tracked
  locally by the two date comparisons — which need no model at all — and appear in the brief
  as title-only lines, with their bodies never sent.

**What would change this:** a local model good enough for the judgement work. The architecture
does not assume the API — the brief generator takes a model endpoint as configuration. If
capable local inference becomes practical on this hardware class, row 1 disappears without a
redesign. That is the main reason the boundary sits where it does.

## Row 2 — the private GitHub remote

**What this costs you:** the complete record — every thread, every brief, the full history —
sits readable on infrastructure you do not control. GitHub encrypts at rest and the repo is
private, but "private" is an access-control setting on someone else's machine, and visibility
is a two-click change.

**Why it is accepted:** the host is a headless box at a remote location with no physical
access. If its disk fails, an unbacked-up thread store is gone, and the memory layer's entire
value proposition is that it compounds over years. A chosen, documented backup beats an
undocumented single point of failure.

**What is done about it:**

- The agent does not hold the credential and does not perform the push. A separate timer
  running as you does it. A manipulated agent has no route to this channel.
- `gitleaks` runs pre-push and fails the push on any credential-shaped match.
- The private repo has **no** GitHub Actions, no Pages, and no collaborators. CI runs on the
  public repo against synthetic fixtures only. Actions logs are a content-leak path and this
  repo is never given one.
- Should the trade stop feeling right, `git-crypt` over a `threads/sensitive/` subdirectory
  encrypts the small fraction that warrants it without making the rest unreadable to you.

## Row 3 — the calendar read

Lowest-stakes row on the ledger: the request goes to the party that already holds the data.
Scoped to `calendar.readonly` against a single calendar id, so the token cannot write, cannot
delete, and cannot reach your other calendars.

## Row 4 — the failure notification

Included for completeness rather than because it is risky. The payload is a constant string
plus today's date, sent to a public ntfy topic only when the morning brief is missing. It
carries no thread content, no titles, and no calendar data.

Two things worth noting anyway. The topic name is the only access control ntfy offers, so it
must be a long random string rather than something guessable — anyone who knows the topic can
subscribe. And an observer of that topic learns exactly one bit about you: that your machine
failed to produce a brief on a given day. That is an acceptable leak for a watchdog whose
entire value is being simpler than the system it watches; a self-hosted notification channel
would remove even that bit at the cost of the watchdog depending on more infrastructure, which
is the wrong trade for this component specifically.

## What is deliberately absent

| Not present | Why |
| --- | --- |
| Email, in v1 | Deferred to v1.5. It is the largest work item and the only source of attacker-controlled text. |
| Any send capability | The agent drafts; you send. Not a setting — simply not built. |
| A hosted dashboard | The brief is a markdown file. A phone-readable view can render from the private repo without another egress path. |
| Analytics of any kind | There is one user. |

## Maintenance

Adding a capability means adding a row here **first**. If a change adds an egress path and this
table is not updated in the same commit, the change is incomplete. The table should make you
slightly uncomfortable to read; that discomfort is the signal that it is accurate.
