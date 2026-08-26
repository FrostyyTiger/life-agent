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
| 4 | A fixed string: *"No brief for YYYY-MM-DD"* (now checks the mail digest — see [mail-v1](plans/mail-v1.md) stage 8) | ntfy.sh | Only on failure | Dead-man's switch at 08:00 | **No personal data.** A date and the fact that a file is missing. |
| 5 | Gmail API reads | Google | Every 15 min | mail-sync timer | Read-only (`gmail.readonly`) query against your own inbox. Google already has this data. |
| 6 | `BAAI/bge-m3` model weights | Hugging Face | Once (first `mail embed` run) | mail-sync/embed | **No personal data.** A public model's weights, not your data. |
| 7 | Headers + truncated body of *new* mail only | Anthropic API | Every 15 min | mail-tag timer | **Yes — personal.** Whatever's in the mail, truncated to `config.mail.body_chars_for_model`. |
| 8 | Tagged-mail summaries (not raw bodies) | Anthropic API | Daily, 06:30 | mail-digest timer | **Yes — personal**, but already-summarized by row 7's pass, not the raw mail again. |
| 9 | The composed digest | Google (`gmail.insert`, to your own inbox) | Daily, 06:30 | mail-digest timer | **Yes — personal**, but it's a summary of your own mail being written back to your own mailbox — the scope cannot send it, or write, anywhere else. |

Nothing else leaves. Specifically: no telemetry, no analytics, no crash reporting, no
third-party dependencies that phone home, and the agent holds no credential for any
destination other than rows 1, 5-9's endpoints (calendar brief and mail-v1
respectively — two separate credential sets, held by the same `life-agent` identity).

The mail **archive itself** (`mail.db`, embeddings, chunk text) never leaves the host and
is not part of either git repo — it lives in `$LIFE_AGENT_STATE`, outside every
repository, exactly like credentials live outside every repository (see
`trust-model.md`'s boundary diagram). Only what rows 5-9 describe ever crosses the
machine boundary; a search or `show` over the query socket answers locally and sends
nothing anywhere.

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

## Rows 5-9 — mail-v1

Same trade as row 1, paid twice over: mail content, not just calendar/thread content,
now transits a frontier API. What's done about it mirrors row 1's approach rather than
inventing a new one:

- **Only new mail is sent, not the whole archive.** Row 7 (tagging) sees each mail once,
  truncated; row 8 (digest) sees already-produced tag summaries, never raw bodies a
  second time. A search over the query socket (`serve.py`) never calls a model at all
  in `--mode fts`, and even `--mode vec`/`hybrid` only ever computes a local embedding —
  nothing about a search query leaves the host.
- **The model in this path has no tools and cannot act** — see
  `trust-model.md`'s "Mail: prompt injection" section for the full accounting. This
  matters for egress specifically because it means the worst case of row 7/8 going wrong
  is a wrong tag or summary, not a new, attacker-directed egress path.
- **Row 9 can only ever write to one place**: the owner's own inbox. The `gmail.insert`
  scope has no send capability at all, and the recipient is a constant in `digest.py`,
  never model output — there is no way for row 9 to become "send this summary somewhere
  else."
- Both mail-specific credentials (Gmail, the `claude -p` subscription token) are held by
  `life-agent`, not the owner's account — a manipulated *owner-side* session (every other
  Claude Code session on this host) has no route to any of rows 5-9's endpoints, the
  same separation-of-principals argument row 2 makes for the publisher.

**What would change this:** same answer as row 1 — a local model good enough for
tagging/digest judgement removes rows 7-8. Row 5 (Gmail read), row 6 (one-time model
download), and row 9 (insert) are comparatively low-stakes and would likely remain even
then.

## What is deliberately absent

| Not present | Why |
| --- | --- |
| Automatic thread detection from mail | The other half of the original v1.5 milestone — mail-v1 shipped the archive/digest half first; see `docs/plans/mail-v1.md`. Needs v1's thread store, which doesn't exist yet. |
| Any send capability | The agent drafts or inserts to your own inbox; it never sends. Not a setting — simply not built. |
| Attachment downloads | Mail-v1 records filename/mimetype/size only, never content. A stated non-goal, not a gap. |
| A hosted dashboard | The brief and the mail digest are both markdown files. A phone-readable view can render from the private repo without another egress path. |
| Analytics of any kind | There is one user. |

## Maintenance

Adding a capability means adding a row here **first**. If a change adds an egress path and this
table is not updated in the same commit, the change is incomplete. The table should make you
slightly uncomfortable to read; that discomfort is the signal that it is accurate.
