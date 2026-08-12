# src/

**Not yet implemented.** This directory currently describes the modules rather than containing
them. The design is settled (see [`../docs/architecture.md`](../docs/architecture.md)); the
code is not written.

Recording it this way rather than committing stubs is deliberate — an empty function that
returns `None` looks like progress and is worse than an honest gap.

## Planned modules

| Module | Responsibility | Network? |
| --- | --- | --- |
| `threads.py` | Parse, validate and write thread files. Loud on malformed frontmatter. | no |
| `alarms.py` | The two date comparisons. Pure functions, no I/O. | no |
| `calendar.py` | Google Calendar read, one id, read-only scope. | yes |
| `compose.py` | Model call for the brief. Degrades to a title-only brief on failure. | yes |
| `infer.py` | Model call proposing new threads. Skipped entirely on failure. | yes |
| `confirm.py` | Apply `keep`/`drop` replies to frontmatter. | no |
| `main.py` | Wire the daily pass together, commit at the end. | — |

## Constraints the implementation must honour

- **`alarms.py` must not import anything that touches the network.** The lead-time and
  staleness comparisons are what the user actually depends on; they must work when the API,
  the calendar, and the internet are all unavailable. Keeping the dependency direction
  enforceable at import level is the cheapest way to guarantee that.
- **`LIFE_AGENT_DATA` has no default.** Refuse to start if unset. A fallback path is how a
  test run ends up writing to a real thread store.
- **Every run commits**, including runs that changed nothing. The commit history is the audit
  trail described in the trust model, and gaps in it are indistinguishable from silent
  failures.
- **Never delete a thread file.** `state: dropped`, always.
- **Preserve human-written body text.** The agent may append a dated line; it may not rewrite
  or remove what the user wrote. This is a soft boundary — see the trust model's honest
  accounting of which limits the kernel enforces and which it does not.
- **`briefs/` is append-only.** Hash existing brief files before the run and fail loudly if
  any changed.

## Tests

The public repo's fixtures in [`../examples/threads/`](../examples/threads/) are a synthetic
life covering all four failure modes plus one proposed thread. CI must run the brief generator
against them with the model call stubbed. Without that, the public skeleton quietly becomes
something that only works on the author's machine against the author's data, and nobody finds
out until a stranger clones it.
