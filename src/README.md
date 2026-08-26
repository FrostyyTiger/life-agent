# src/

This directory holds two things at very different stages. `src/mail/` (below) is real,
tested code — see [`../docs/status/mail-v1.md`](../docs/status/mail-v1.md). Everything
else in this file describes the original calendar-brief system, which remains **not yet
implemented**: the design is settled (see
[`../docs/architecture.md`](../docs/architecture.md)), but no code exists for it, and
this section still just describes the modules rather than containing them.

Recording it this way rather than committing stubs is deliberate — an empty function that
returns `None` looks like progress and is worse than an honest gap.

## src/mail/ (implemented)

The mail archive, tagging, and morning digest — see
[`../docs/plans/mail-v1.md`](../docs/plans/mail-v1.md) for the design and
[`../docs/status/mail-v1.md`](../docs/status/mail-v1.md) for what's verified and what
remains (owner credentials + a `sudo` bootstrap run). Invoked as `bin/mail <command>`,
which wraps `uv run python -m src.mail.cli`.

| Module | Responsibility | Network? |
| --- | --- | --- |
| `cli.py` | Argument parsing/dispatch; falls back to the query socket when `mail.db` isn't directly readable | no (dispatches to modules that do) |
| `config.py` | `LIFE_AGENT_*` env vars + `config.yaml` loading. No defaults, refuses to start if unset. | no |
| `store.py` | SQLite schema, migrations, all CRUD (messages, tags, feedback, rules, digests) | no |
| `gmail.py` | OAuth (readonly + insert), sync (backfill, incremental, history-expired fallback) | yes |
| `extract.py` | Raw RFC 822 bytes → header/body fields; HTML→text | no |
| `embed.py` | Chunking + `BAAI/bge-m3` embeddings (GPU, CPU fallback) | yes (one-time model download) |
| `search.py` | FTS5 (`bm25()`), sqlite-vec KNN, and reciprocal-rank-fusion hybrid search | no |
| `claude_cli.py` | `claude -p --bare --tools ""` subprocess wrapper for tagging/digest | yes (via the subprocess) |
| `tag.py` | Mute/VIP rules, prompt construction (with block-boundary sanitization), schema validation | no (calls `claude_cli`) |
| `digest.py` | Section bucketing, numbering, prose composition, Gmail insert, git commit | yes (via `claude_cli` + Gmail insert) |
| `feedback.py` | Parses owner replies to digests into `feedback`/`rules` | no |
| `serve.py` | The read-only query socket (`/status`, `/search`, `/show`) | no |
| `socket_client.py` | Owner-side client for the above | no (local Unix socket only) |

Constraints this package actually honours, verified by its own test suite
(`tests/mail/`): no tools and no session persistence for any mail-path model call;
every tag/digest reply schema-validated independently of the provider's own
enforcement; untrusted mail content never used to identify block boundaries in a
prompt; feedback accepted only from replies to a `Message-ID` the archive itself
generated. See `docs/trust-model.md`'s "Mail: prompt injection" section.

## Planned modules (calendar v1 — not yet implemented)

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
against them with the model call stubbed, once that generator exists. Without that, the
public skeleton quietly becomes something that only works on the author's machine against
the author's data, and nobody finds out until a stranger clones it.

`tests/mail/` follows the same principle for the mail package, and already does this today:
[`../examples/mail/`](../examples/mail/) is a 12-message synthetic mailbox (including a
prompt-injection attempt and a digest-reply carrying feedback commands) that every stage's
tests run against — Gmail and `claude -p` are both faked (`tests/mail/fake_gmail.py`, a real
executable on `PATH` for `claude_cli.py`) rather than mocked at the Python boundary, so the
actual subprocess/API contracts get exercised, not just the business logic around them.
