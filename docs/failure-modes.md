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
a brief file exists and is non-empty.

```
07:00  life-agent-brief.timer      → agent user, generates today's calendar brief (not yet built)
07:30  life-agent-publish.timer   → you, commits + pushes the data repo
08:00  life-agent-deadman.timer   → you, checks briefs/<today>-mail.md exists; pushes to your phone if not
```

Three timers, three responsibilities, no shared failure. The check runs as you, not as the
agent, so an agent that cannot start at all — bad permissions, expired token, disk full — is
still detected. `life-agent-deadman.sh` currently watches the **mail digest**
(`briefs/<today>-mail.md`), not the calendar brief this section otherwise describes: the
calendar system (`life-agent-brief.timer` → `src/main.py`) was never implemented, and
mail-v1 (`docs/plans/mail-v1.md`) is what actually runs on this host. Mail-v1 adds three
more timers of its own (`life-agent-mail-{sync,tag,digest}`), all as the agent user — see
`setup/README.md`.

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
| 12 | Prompt injection via mail | Not reliably detectable as it happens | No tools + schema-validated output bound the worst case to a wrong tag or summary — see `trust-model.md`'s "Mail: prompt injection" section |
| 13 | A Gmail token (readonly or insert) expires or is revoked | `mail sync`/`digest` logs an auth error; `mail tag` is unaffected (doesn't use Gmail) | Re-authenticate: `mail auth readonly` / `mail auth insert` from a PC via `ssh -L 8765:localhost:8765`; bootstrap's chown hands the new token back to `life-agent` |
| 14 | Gmail history expired (roughly >7 days since the last successful sync) | `history.list` returns 404 | `gmail.py` falls back automatically to `messages.list(q="after:...")` for the last 2 days — logged as `mode=incremental-fallback`, not a failure requiring action |
| 15 | The `BAAI/bge-m3` download fails (no network, HF down) | `mail embed`/`sync` raises, logged | Retried next run; `--mode fts` search is unaffected meanwhile — only `vec`/`hybrid` degrade to unavailable |
| 16 | GPU unavailable to the embedding step | `SentenceTransformerEmbedder` logs a loud warning, runs on CPU | Slower, not broken; nothing else on the host depends on the GPU |
| 17 | `claude -p` is rate-limited, down, or the subscription token expired | `ClaudeCliError` | Tagging retries next run (up to 3 attempts per message, then `category=unknown`, terminal); digest composition degrades to mechanical lines from the tags alone — same principle as failure 3's degraded brief |
| 18 | The digest's Gmail insert fails | Logged loudly to stderr, `insert_error` recorded in the `digests` row | The brief **file already exists by that point** — the dead-man's switch (which only checks the file) is satisfied regardless. The missing inbox copy is a separate, lower-severity gap, visible in the journal or by checking `digests.inserted_gmail_id` |

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

**Mail digest, same principle (failure 17):** if the Sonnet call fails or returns the wrong
number of lines, `digest.py` falls back to a mechanical line built straight from each tagged
message's `summary`/`action`/`deadline` — no model, no prose, a `_Degraded_` marker at the
top of the file. If there are no tagged messages at all (tagging itself is behind, or this
is the very first run), it degrades one step further to a flat list of raw subjects. Either
way, a file gets written; the dead-man's switch's one job — stat a file — never depends on
any of this succeeding.

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

- **A compromised host.** Root defeats all of this.
- **A wrong `lead` value.** If you say a passport renewal needs two weeks and it needs eight,
  the system faithfully tells you too late. Lead times are a judgement the human makes, and
  reviewing them after a near-miss is the only correction available.
- **Things that were never a thread.** The agent knows about your calendar and your mail. A
  commitment made verbally and never written anywhere is invisible to it.
- **A model that complies with an injected instruction.** Prompt injection itself — the model
  being *asked* to do something by mail content — is not detectable, and mail-v1 doesn't
  pretend otherwise. What's defended is the *consequence*: no tools and schema-validated
  output mean compliance can only produce a wrong tag or a misleading summary. See
  `trust-model.md`'s "Mail: prompt injection" section for the full accounting — this is no
  longer an open item the way it was before mail-v1 existed.
