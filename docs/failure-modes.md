# Failure modes

The failure this system must survive is not a crash. It is **silence** — the brief quietly
stops arriving, you stop expecting it, and three weeks later you miss something that mattered.
A tool you rely on for remembering must never fail by being forgotten.

Everything below follows from that.

## The dead-man's switch

The rule: **no brief by 08:00 means something is wrong, and the absence itself must raise the
alarm.**

The alarm cannot live inside the agent. A process that failed to run also fails to report that
it failed to run — that is the entire class of bug being defended against. So detection is a
separate systemd timer, owned by a different principal, that knows only one thing: whether
`briefs/YYYY-MM-DD.md` exists and is non-empty.

```
07:00  life-agent-brief.timer     → agent user, generates today's brief
07:30  life-agent-publish.timer   → you, commits + pushes the data repo
08:00  life-agent-deadman.timer   → you, checks the file exists; pushes to your phone if not
```

Three timers, three responsibilities, no shared failure. The check runs as you, not as the
agent, so an agent that cannot start at all — bad permissions, expired token, disk full — is
still detected.

The dead-man's switch has no dependency on the model API, the network, or the agent's own
code. It stats a file. That is deliberate: the watchdog must be the simplest component in the
system, because nothing watches the watchdog.

## Failure table

| # | Failure | Detection | Response |
| --- | --- | --- | --- |
| 1 | Brief job does not run | Dead-man's switch at 08:00 | Push notification. Manual run to diagnose. |
| 2 | Brief job runs, produces nothing | Same — empty file counts as absent | Same |
| 3 | Model API down or rate-limited | Job catches, writes a degraded brief | **Degraded brief still ships** (see below) |
| 4 | Calendar token expired | Job catches; degraded brief notes the calendar is stale | Reauthorise; token expiry is itself a thread |
| 5 | Agent invents a thread | You read the brief | `drop` in the brief; `state: dropped` prevents re-proposal |
| 6 | Agent corrupts a thread body | `git diff` on the daily commit | `git revert`. This is why every run commits. |
| 7 | Agent edits a past brief | Pre-run check hashes existing briefs, fails loudly | Investigate — a soft boundary was crossed |
| 8 | Push to remote fails | Publisher timer exit code | Retry next day; local history is intact meanwhile |
| 9 | `gitleaks` finds a secret | Pre-push hook, push blocked | Rotate the credential, then scrub history |
| 10 | Disk full | Brief write fails → dead-man's switch | Notification. `briefs/` grows ~2 KB/day; not a near-term risk. |
| 11 | You ignore the brief for two weeks | Not detected, by design | See *Neglect* below |
| 12 | Prompt injection via email (v1.5+) | Not reliably detectable | Out of scope for v1 because email is out of scope for v1 |

## Degraded operation

Failure 3 deserves its own rule, because the tempting behaviour is wrong.

If the model API is unavailable, the job **must still write a brief**. The two alarms — lead
time and staleness — are pure date arithmetic and need no model whatsoever. A degraded brief
lists exactly what tripped, with titles and dates and no prose, and a header saying it is
degraded.

This matters more than it looks. It means the core function of the system — noticing — has no
dependency on the network at all. The model makes the brief pleasant to read and does the
inference of new threads; it is not load-bearing for the part you actually depend on. If it
were, an API outage would silently become a missed deadline.

## Neglect

Failure 11 is not a bug to fix. If you ignore the system for two weeks it must pick up
cleanly and not greet you with sixty items of accumulated debt, because a report that makes
you feel behind is a report you stop opening — at which point it has failed completely, while
appearing to work perfectly.

Concretely:

- The brief shows **at most three** items in *Needs you today*. Never more, regardless of how
  many alarms tripped. Everything else falls to a collapsed list with a count.
- Stale threads are stated neutrally with elapsed time. "No movement in 18 days", not
  "overdue" or "⚠️".
- Alarms do not compound. A thread that has been stale for a month is not more urgent than one
  stale for a week; it appears once, either way.
- After a gap, the first brief back opens with a short *while you were away* summary rather
  than replaying every brief you missed.

## What is not defended against

Stated so the boundaries are not mistaken for coverage:

- **Prompt injection**, until v1.5 introduces untrusted text. The mitigation strategy has to be
  written before that code is, and the trust model's soft-boundary section is where it will go.
- **A compromised host.** Root defeats all of this.
- **A wrong `lead` value.** If you say a passport renewal needs two weeks and it needs eight,
  the system faithfully tells you too late. Lead times are a judgement the human makes, and
  reviewing them after a near-miss is the only correction available.
- **Things that were never a thread.** The agent knows about your calendar and, later, your
  mail. A commitment made verbally and never written anywhere is invisible to it.
