# Architecture

## Shape

```
                    ~/.config/life-agent/        (0600, no git)
                    ├── google-oauth.json
                    └── api-key
                              │ read
                              ▼
  Google Calendar ──read──▶ ┌──────────────────┐ ──▶ Anthropic API
  (one id, readonly)        │   brief job      │     (threads that tripped
                            │  user: life-agent│      an alarm + calendar)
                            └──────────────────┘
                              │ read+write          
                              ▼                     
                    life-agent-data/   (private git)
                    ├── threads/     agent rw · you rw
                    ├── briefs/      agent append · you read
                    └── config.yaml  agent r · you rw
                              │
                              │ commit (agent) ─── no remote credential
                              │ push  (you) ─────▶ GitHub, private
                              ▼
                    08:00 dead-man's switch (you) ──▶ phone, only on absence

  life-agent/  (this repo, public)   agent: read + execute only, never write
```

## Components

| Component | Runs as | Schedule | Responsibility |
| --- | --- | --- | --- |
| `brief` | `life-agent` | 07:00 daily | Read calendar, evaluate alarms, generate brief, commit |
| `confirm` | `life-agent` | on demand | Apply your `keep`/`drop` replies to thread frontmatter |
| `publish` | you | 07:30 daily | Secret-scan, then push the data repo |
| `deadman` | you | 08:00 daily | Assert today's brief exists; notify on absence only |

Four small programs rather than one daemon. Nothing is always-on: the "always-on machine" in
the pitch is the host, not a resident process. A crashed daemon is a silent failure mode; a
timer that did not fire is caught by the dead-man's switch.

## The daily pass

1. **Load.** Parse every file in `threads/`. Reject malformed frontmatter loudly rather than
   skipping it silently — a thread that fails to parse is a thread that cannot alarm.
2. **Evaluate.** Pure date arithmetic, no model:
   - dated: `due` set and `today >= due - lead`
   - stale: `today >= last_touched + patience`
   - `confidence: proposed` threads are skipped; they cannot alarm until confirmed.
   - `done` and `dropped` are skipped.
3. **Fetch calendar.** Today plus a 14-day lookahead, one calendar id, read-only.
4. **Infer.** Ask the model whether anything in the calendar or repo activity looks like an
   open thread with no file. Write those as `confidence: proposed`.
5. **Compose.** Model writes the brief from the tripped alarms, the calendar, and the
   proposals. Hard cap of three items under *Needs you today*.
6. **Write and commit.** `briefs/YYYY-MM-DD.md`, then commit everything changed this run.

Steps 1, 2 and 6 have no network dependency. If step 3, 4 or 5 fails, the job writes a
degraded brief from step 2 alone and still commits — see
[`failure-modes.md`](failure-modes.md).

## Brief format

```markdown
# Tuesday 12 August

## Needs you today
- **Passport renewal** — expires 4 Nov, slots run 4 weeks out and the form needs a
  countersignature. Book this week.
- **Roof gutter quote** — Hartmann have been silent 11 days. You said you'd chase at ten.

## Coming up
- Thu 14th, dentist 14:30
- Mon 18th, scaffolding delivery window

## Gone quiet
- Synth firmware rewrite — no movement in 23 days

## Proposed
- "Insurance renewal call"? — from a calendar entry on 20 Aug with no thread. keep / drop
```

Four sections, fixed order, three-item cap on the first. The *Proposed* section is the capture
loop: it is how threads get created without you having to write anything down.

## Configuration

`config.yaml` in the data repo — yours to edit, agent reads only:

```yaml
calendar_id: primary
lookahead: 14d
defaults:
  patience: 14d
  lead: 7d
brief:
  max_urgent: 3
  time: "07:00"
exclude_tags: [medical, finance]   # titles only; bodies never sent to the model
```

## Why the layers from the original pitch collapsed

The pitch described a memory layer, a craft layer, and an action layer. Building it revealed
that for v1 they are not three things:

- The **memory layer** is `threads/` plus git. It is the product.
- The **craft layer** is a prompt and the three-item cap. Distilled knowledge of how to write
  a brief that gets read, and nothing more.
- The **action layer** is one write to one directory. Everything else it might have done —
  sending, scheduling, replying — is exactly what the trust model forbids.

The layers reappear as the system grows, but building them as three subsystems up front would
have produced scaffolding around an unbuilt product. The reduction is the design decision, and
it is logged as such in the private notes.
