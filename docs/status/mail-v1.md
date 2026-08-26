# mail-v1 status

Tracks what works, how it was verified, and what's left, one section per stage. See
[`../plans/mail-v1.md`](../plans/mail-v1.md) for the plan this implements.

## Stage 1 — skeleton, config, tooling

**Done.**

- `pyproject.toml`: `uv`-managed, Python 3.12, `tool.uv.package = false` (this is a CLI
  app — `src/mail` is never imported as an installed package, only run as
  `python -m src.mail.cli`). Dependencies for the whole plan are declared now
  (`google-api-python-client`, `google-auth-oauthlib`, `pyyaml`, `beautifulsoup4`,
  `sqlite-vec`, `sentence-transformers`, `torch`, `numpy`) even though only `pyyaml` is
  used yet, so `uv sync` is exercised once against the real CUDA wheel set on this host
  rather than deferred to stage 5.
- `src/mail/config.py`: `load_env()` reads `LIFE_AGENT_DATA` / `LIFE_AGENT_STATE` /
  `LIFE_AGENT_CONF`, refuses with a clear error naming every missing var (no defaults,
  by design). `load_config()` reads the `mail:` section of `$LIFE_AGENT_DATA/config.yaml`
  — requires `address` and `tag_since`, defaults the rest (`max_needs_you=5`,
  `body_chars_for_model=4000`, etc).
- `src/mail/cli.py`: `mail status` — prints the three env dirs, the DB path, and a
  message count. Works against a DB that doesn't exist yet, or one that exists but has
  no `messages` table yet (both print `0` with a hint), so stage 1 doesn't depend on
  stage 2's schema. `bin/mail` wraps `uv run python -m src.mail.cli`.
- Real config wired: `$LIFE_AGENT_DATA/config.yaml` (private repo) now has a `mail:`
  section with the real address, an optional `owner_name` (used only by the leak test
  below), and `tag_since: 2026-08-20`, per the kickoff note. `examples/config.yaml`
  (public) documents the same section with placeholder values.
- `examples/mail/*.eml`: 12 synthetic fixtures — plain text, HTML-only, multipart with
  an attachment, German plain text, a second English plain (distinct sender), a
  receipt, a second multipart/alternative (conference CFP), a prompt-injection attempt,
  a reply-to-digest carrying feedback commands (`#3 junk`, `vip …`, `mute …`, `topic …`,
  `reply #2: …`), a mail from the owner's own address (for `is_from_owner`), an RFC 2047
  encoded-word subject + display name, and a minimal-body mail with Cc/Reply-To. All
  synthetic — no real names, addresses, or content.
- `tests/mail/`: env/config error paths, `mail status` against an empty env, an empty
  DB, and a DB with rows; fixture sanity (count, parseability, category coverage, the
  injection string and digest-reply commands are actually present in their fixtures);
  and `test_no_leaks.py`, which reads the real address and `owner_name` from
  `$LIFE_AGENT_DATA/config.yaml` when that env var is set and greps the whole repo tree
  (excluding `.git`, `.venv`, `uv.lock`) for either literal string, failing loudly if
  found. Skips cleanly (not a failure) on a fresh clone where `LIFE_AGENT_DATA` isn't
  set, so CI on the public repo — which has no access to the private config — still
  passes.

**Deliberately not built yet:** the plan's stage-1 bullet mentions "a fake Gmail client
that serves those fixtures." Building that now would mean guessing the shape
`gmail.py` (stage 3) needs before that module exists — the fixtures themselves are
committed and stage 3 will build the fake client against the real interface instead of
a shape invented two stages early.

**Verified:**
- `uv sync` — resolves and installs cleanly, including the CUDA 13 torch wheels for the
  RTX 3070 Ti (`~4 min` cold). No wheel build issues.
- `uv run pytest` — all green, including the leak test run against the real
  `$LIFE_AGENT_DATA/config.yaml`.
- `bin/mail status` with no env vars set → prints `mail: missing required environment
  variable(s): LIFE_AGENT_DATA, LIFE_AGENT_STATE, LIFE_AGENT_CONF …`, exit 2.
- `bin/mail status` with env vars pointed at the real dirs → prints the real address,
  `tag_since`, and `messages: 0 (no database yet — run \`mail sync\`)`, exit 0.

## Stage 2 — store

**Done.**

- `src/mail/store.py`: `connect(db_path)` creates `$LIFE_AGENT_STATE` (mode `0700`) and
  the db file if needed, sets WAL + foreign keys, loads the `sqlite-vec` extension, and
  applies migrations tracked in a `schema_migrations` table (so re-opening an existing
  db is a no-op, and future schema changes are additive migrations rather than a
  rewrite).
- Full schema in one migration: `messages`, `messages_fts` (FTS5, external content on
  `messages.rowid` with insert/update/delete triggers keeping it in sync), `chunks` +
  `vec_chunks` (`sqlite-vec`, `float[1024]`, rowid-linked to `chunks.id`), `tags`,
  `feedback`, `rules`, `digests`, `sync_state`, `pending`.
- `upsert_message`/`get_message`/`mark_deleted`/`count_messages`. Deletions are never
  physical — `mark_deleted` sets `deleted_at`; re-upserting an existing message (as a
  resync would) never clears it, so a message can't be silently "undeleted" by a stale
  refetch.
- `cli.py`'s `mail status` now reports a real message count via `store.connect` instead
  of a stage-1 placeholder query; first run creates the database and says so.

**Verified:**
- `uv run pytest` — 30 passed (11 new store tests: connect/idempotence,
  upsert/get/update, delete-preserves-row, re-upsert-does-not-undelete, FTS finds an
  inserted message and stops finding it after the subject changes, `vec_chunks` accepts
  a `sqlite-vec` embedding).
- `bin/mail status` against the real `$LIFE_AGENT_STATE` — creates
  `/…/life-agent-state/mail.db` (`0700` dir) on first run, reports `messages: 0
  (database just created — run \`mail sync\`)`.

**Left for later stages:** `LIFE_AGENT_STATE` is currently owned by the owner account
(created by this session, which runs as the owner) — stage 8's bootstrap reassigns it
to `life-agent:life-agent` once that user exists. Nothing writes real data into it yet;
stages 3-7 do.

## Stage 4 — text extraction + full-text search

**Done** (built ahead of stage 3, per the plan's stated order — extraction and search
only need raw RFC 822 bytes, not a live Gmail connection).

- `src/mail/extract.py`: `build_message_fields(raw_bytes)` parses with
  `email.policy.default` (which does RFC 2047 header decoding for free — no bespoke
  decoding code needed) and returns the header/body columns `store.py` wants. Body
  prefers `text/plain`; falls back to HTML converted to text via BeautifulSoup with
  `<script>`/`<style>`/`<img>` stripped (removing images removes tracking pixels as a
  side effect — v1 doesn't download or display images at all) while anchor text
  survives naturally. Attachments are recorded as filename/mimetype/size only, content
  never touched.
- `src/mail/search.py`: `search_fts()` — FTS5 `bm25()` with subject weighted 5x over
  `from_addr`/`body_text`; `--from` (substring on `from_addr`), `--since`/`--until`
  (`YYYY-MM` or `YYYY-MM-DD`, `since` inclusive, `until` exclusive of the day/month
  after); excludes soft-deleted rows. `search()` dispatches on `mode`, raising a clear
  `SearchError` for `vec`/`hybrid` until stage 5 exists.
- `cli.py` gained `mail search QUERY [--mode] [--from] [--since] [--until] [--limit]
  [--json]` and `mail show ID [--json]`.
- Fixed a fixture bug the new extraction tests caught: `002-html-only-newsletter.eml`
  wasn't actually HTML-only — it had a `multipart/alternative` plain-text fallback from
  using `add_alternative()`, so extraction correctly preferred the plain part and the
  "HTML-only" test failed for the right reason. Rebuilt it as a genuine single-part
  `text/html` message.

**Verified:**
- `uv run pytest` — 58 passed. New: 10 extraction tests (plain, HTML→text with
  scripts/images stripped and link text kept, multipart/alternative preference, German
  umlauts round-trip, RFC 2047 subject + display name decoded exactly, attachment
  metadata with no content, headers/reply-chain, Cc/Reply-To, the injection fixture's
  text extracted as inert data); 11 search tests (subject match, umlaut body match,
  subject-outranks-body ranking, `--from`, `--since`/`--until` at month granularity,
  `--limit`, deleted rows excluded, mode dispatch, invalid date filter); 7 new CLI tests
  for `search`/`show` (hit rendering, no-results, `--json`, unavailable mode, missing
  id).

**Left for stage 3:** nothing in `extract.py`/`search.py` depends on it, by design —
stage 3 will call `extract.build_message_fields()` per Gmail message and add the
Gmail-only columns (`id`, `thread_id`, `history_id`, `internal_date`, `labels_json`,
`size`, `is_from_owner`, `fetched_at`) before `store.upsert_message()`.

**Babysitter fix folded in:** `search_fts` was passing the raw query straight to FTS5's
`MATCH`, so punctuation the user typed as data (`foo-bar`, `invoice: hetzner`, a stray
`"`) raised an FTS5 syntax error instead of searching for it. `search.py` now tokenizes
the query and quotes each term as its own FTS5 string literal (escaping inner quotes by
doubling them), joined with spaces — implicit AND — with a trailing `*` kept outside
the quotes for prefix search. `sqlite3.OperationalError` from the `MATCH` is also caught
and re-raised as `SearchError`, as a second line of defense. Four new tests
(punctuation, a stray quote, terms-are-ANDed, prefix search); three existing tests that
had been (ab)using FTS's `OR` operator to combine several fixtures into one query were
rewritten — two against a small set of synthetic messages sharing a common word instead
of the real fixtures, since AND-of-literal-terms means that trick no longer works, and
real user queries were never going to look like `"word1 OR word2 OR word3"` anyway.

## Stage 3 — Gmail auth + sync

**Done** (code + fake-client tests, per the plan's order — the real run needs
NEED-MARCEL items 1/2/4 below).

- `src/mail/gmail.py`: sync logic (`sync()` and everything it calls) is written
  against a small duck-typed `GmailPort` interface — `get_profile`,
  `list_all_message_ids`, `list_recent_message_ids`, `get_messages`, `collect_history`
  — rather than directly against `googleapiclient`'s dynamically-built `Resource`
  objects. `RealGmailPort` wraps the real API (batched fetch via
  `BatchHttpRequest`, `num_retries=5` on every `.execute()` for the 429/5xx backoff the
  plan asks for); `tests/mail/fake_gmail.py`'s `FakeGmailPort` serves
  `examples/mail/*.eml` and models Gmail's history/backfill semantics (seed / deliver /
  delete / expire_history_before) well enough to exercise every path in `sync()`. This
  *is* the "fake Gmail client" stage 1 flagged as deferred — built now against the real
  interface `gmail.py` actually needs, not a guessed one.
- **Deviation from the plan text:** messages are fetched with `format="raw"`, not
  `format="full"`. `extract.py` already parses raw RFC 822 bytes; reconstructing
  equivalent bytes from Gmail's `format=full` JSON payload tree would mean
  re-implementing MIME assembly for no benefit, and `snippet`/`labelIds`/`historyId`/
  `internalDate` are present in the message resource either way.
- `sync()`: `pending` (id, added_at) is the single resumable work queue for both the
  initial backfill and any incremental run that hits its budget — draining it is
  always the first thing a sync call does. Backfill: record `profile.historyId` before
  paging `messages.list` so nothing that arrives mid-discovery is missed, queue every
  id into `pending`, then drain in batches of 50 respecting `--budget`; resuming a
  half-drained backfill just continues draining, no re-listing. Incremental:
  `history.list(startHistoryId)` for added/deleted, `HistoryExpired` (404) falls back
  to `messages.list(q="after:<2 days ago>")`. `is_from_owner` compares the extracted
  `from_addr` against `config.mail.address` (case-insensitively).
- `mail auth readonly|insert`: `InstalledAppFlow.from_client_secrets_file` +
  `run_local_server(port=8765, open_browser=False)`, prints the SSH port-forward
  reminder and the Production-publishing-status warning, writes the token `0600`.
  Missing `google-client.json` or a missing/expired token surfaces as `NEED-MARCEL:` on
  stdout/stderr and exit 2, per the kickoff protocol, rather than a stack trace.
- `mail sync [--budget SECONDS]` wired into `cli.py`.

**Verified:**
- `uv run pytest` — 74 passed. New: 9 sync tests against `FakeGmailPort` (full
  backfill, `is_from_owner`, a budget-cutoff mid-backfill and its resume, resuming from
  a `pending` table left by a prior interrupted run, incremental add+delete, a
  no-op incremental run, history-expired fallback, the deadline helpers, the
  internal-date→ISO conversion) plus 2 CLI tests (`mail auth`/`mail sync` without
  credentials both print `NEED-MARCEL:` and exit 2, rather than crashing).
- `bin/mail auth readonly` and `bin/mail sync` against the real `$LIFE_AGENT_CONF`
  (currently empty) — both print the expected `NEED-MARCEL:` line and exit 2.
- `google-api-python-client`, `google-auth-oauthlib`, and everything `gmail.py` imports
  install and import cleanly on this host.

**NEED-MARCEL (blocking the real run only, not the rest of the plan):**
1. Google Cloud: enable the Gmail API, OAuth consent screen (External, scopes
   `gmail.readonly` + `gmail.insert`, **Production** publishing status), OAuth client
   (Desktop app) → `$LIFE_AGENT_CONF/google-client.json`, mode `0600`.
2. `claude setup-token` as the owner → `$LIFE_AGENT_CONF/claude-oauth-token` (`0600`) —
   needed for stage 6/7, not stage 3, but flagging once rather than per-stage.
3. Once (1) exists: `mail auth readonly` and `mail auth insert` from a PC via
   `ssh -L 8765:localhost:8765 <host>`.
4. `mail.address` / `tag_since` are already set (kickoff); `owner_name` was added
   during stage 1's leak-test fix.

**Left for later:** `RealGmailPort` is unverified against a live mailbox — do that as
soon as NEED-MARCEL item 3 lands (`mail sync --budget 60`), per the plan.

## Stage 5 — embeddings + hybrid search

**Done** (code + fixture/stub-embedder tests, per the plan's verification note).

- `src/mail/embed.py`: `SentenceTransformerEmbedder` (`BAAI/bge-m3`, fp16 on CUDA, CPU
  with a loud warning otherwise; `HF_HOME=$LIFE_AGENT_STATE/models`). `build_chunk_texts`
  prepends `Subject:`/`From:` to every chunk (so a fragment of a long body still
  carries who it's from and what it's about) and chunks the body at ~500 tokens via the
  model's own tokenizer, capped at 8 chunks/mail. `embed_pending()` processes messages
  with no chunks yet, newest-first within the last 30 days then oldest-first for the
  rest, respecting `--budget` (safe to interrupt: a message with any chunks is skipped
  on the next run).
- `src/mail/search.py` gained `search_vec` (sqlite-vec KNN over `vec_chunks`, grouped to
  one best-distance-per-message, same `--from`/`--since`/`--until` filters as FTS) and
  `search_hybrid` (reciprocal rank fusion, `k=60`, of the FTS and vec rankings — the
  plan's stated default once embeddings exist). `search()` now requires an `embedder`
  for `vec`/`hybrid` and raises a clear `SearchError` if one isn't supplied, rather than
  the stage-4 placeholder that refused those modes outright.
- `cli.py`: `mail embed [--budget SECONDS]`; `mail search` builds an embedder lazily
  (only when `--mode vec|hybrid`, since constructing it loads the model); `mail sync`
  now calls `embed_pending` with whatever budget remains after the fetch, sharing one
  `--budget` across both as the plan asks.
- Tests use a `FakeEmbedder` (`tests/mail/fake_embedder.py`) — deterministic
  bag-of-hashed-words vectors at the real 1024-dim width, no model download — per the
  plan's explicit "tiny stub embedder (no model download in CI)" instruction.

**Verified:**
- `uv run pytest` — 90 passed in ~10s. New: 7 embed tests (chunk text construction incl.
  the empty-body case, the 8-chunk cap, populating `chunks`/`vec_chunks`, skip-if-
  already-embedded, budget cutoff + resume, recent-then-oldest ordering); 10 more
  search tests (vec search by body vocabulary, `--from` filter, deleted-row exclusion,
  hybrid combining and deduping both rankings, mode-without-embedder errors, unknown
  mode); 3 CLI tests (`mail embed`, `mail search --mode vec` via a monkeypatched
  embedder, and the without-embedder error path — all via `FakeEmbedder`, no download).
- **Caught and fixed before it shipped:** a stale stage-4 CLI test
  (`test_search_vec_mode_reports_not_available`) still asserted `--mode vec` fails
  outright. Now that `cli._build_embedder` actually constructs
  `SentenceTransformerEmbedder`, that test silently started downloading the real
  `BAAI/bge-m3` model from Hugging Face on every `pytest` run — a 3.5-minute full suite
  instead of ~10 seconds, with no test asserting anything about it. Replaced with a
  test that monkeypatches `cli._build_embedder` to force the "no embedder" error path
  instead.
- **Real GPU run deliberately deferred**, not skipped: at the time of this stage the
  host showed swap at 85% full (~3.4/4 GiB) and ~1.6 GiB RAM immediately free — visible
  contention from the other concurrent Claude Code sessions this box runs. Downloading
  a ~2 GB model and running it isn't worth adding memory pressure to a shared,
  no-physical-access host for a check the plan itself schedules alongside the real
  sync ("run the real-host checks … as soon as the readonly token exists"), which is
  still blocked on NEED-MARCEL item 3. Deferred by the executor because of host memory
  pressure; reviewer agreed (2026-08-26). Do this together with the first real
  `mail sync`, logging chunks/sec here once it runs.

## Stage 6 — tagging via Claude Code

**Done** (code + fake-`claude`-binary tests; the real run needs NEED-MARCEL item 2).

- `src/mail/claude_cli.py`: runs `claude -p --bare --model <m> --tools "" \
  --no-session-persistence --output-format json --json-schema <schema>`, prompt on
  stdin, `cwd=$LIFE_AGENT_STATE/claude-cwd`, `HOME=$LIFE_AGENT_STATE/home`,
  `CLAUDE_CODE_OAUTH_TOKEN` read from `$LIFE_AGENT_CONF/claude-oauth-token` and injected
  as an env var only — confirmed via a test that spies on the actual `subprocess.run`
  call that the token string never appears in argv. `claude` is invoked by bare name
  (resolved via `PATH`), never a hardcoded path, since the real path lives under the
  owner's nvm tree and is host-specific. Non-zero exit, invalid JSON, and an
  `is_error: true` envelope all raise a clear `ClaudeCliError`.
- **Assumption flagged for real verification:** `--json-schema`'s structured output was
  observed (via a real `claude -p` call against this host, with no auth configured) to
  land in the JSON envelope's `result` field as a *string* that itself needs parsing,
  not a nested object — the auth failure that call hit prevented seeing an actual
  successful schema-shaped reply. `claude_cli.run()` handles both shapes (parses
  `result` again if it's a string, passes it through if it's already a dict), but this
  needs confirming against a real successful call once NEED-MARCEL item 2 lands.
- `prompts/tag.md` + `src/mail/tag.py`: schema (`category` enum, `importance` 0-3,
  `summary` ≤200 chars, `action` ≤120 chars or null, ISO `deadline` or null, `people`)
  validated independently of whatever `--json-schema` enforced, since how strictly the
  CLI enforces the schema itself is unverified (same reason as above). Each `<mail
  id="…">` block is told explicitly that its content is untrusted data, not
  instructions. Mute rule skips the model entirely (`category=junk`, `model=rule:mute`);
  VIP rule floors importance to ≥2 *after* the model call, per the plan. A message that
  fails validation 3 times gets `category=unknown` and stops being retried; anything
  under 3 attempts is retried on the next `mail tag` run.
- `select_messages_to_tag`: `internal_date >= tag_since`, not from the owner, not
  deleted, not already successfully tagged or exhausted, and — reserving a naming
  convention stage 7 will actually produce — not one of the archive's own digest mails
  (`message_id_hdr LIKE '<digest-%'`).
- `mail tag [--limit N]` wired into `cli.py`.

**Verified:**
- `uv run pytest` — 113 passed. New: 8 `claude_cli.py` tests against a real fake
  `claude` executable placed on `PATH` (round trip, non-zero exit, malformed stdout,
  `is_error` envelope, missing token, the token-never-in-argv spy test); 13 `tag.py`
  tests (selection filters including the digest-mail exclusion and the 3-attempt
  retry/give-up cycle, mute/VIP rules, a successful round trip, a `ClaudeCliError`
  marking a whole batch as one failed attempt each, a malformed reply being rejected
  without crashing, **the prompt-injection fixture** tagged correctly both via a
  python-level fake runner and via the real fake-binary subprocess path — the injected
  instructions have no effect because there is nothing in the pipeline capable of
  acting on them); 2 CLI tests.
- **Bug caught while writing these tests:** `tests/mail/conftest.py`'s
  `make_message()` fixture had a `date_iso` of `2026-08-15` but an `internal_date`
  that actually corresponded to `2025-08-15` — a year off, unnoticed until
  `select_messages_to_tag`'s `internal_date >= tag_since` filter started excluding
  every default-constructed test message. Fixed at the source (the shared fixture),
  not by patching each affected test.
- `bin/mail tag --limit 5` on the real host (empty archive, no token yet) — reaches the
  `claude` binary resolution correctly (`which claude` succeeds via `PATH`) and returns
  `tagged=0 muted=0 failed=0` cleanly since there's nothing to tag yet.

**NEED-MARCEL:** item 2 (`claude setup-token` → `$LIFE_AGENT_CONF/claude-oauth-token`)
is what's blocking a real tagging run — flagged in stage 3's section, still open.

**Babysitter fix folded in:** `_render_mail_block` interpolated subject/from/body
verbatim, so a mail whose body contained a literal `</mail><mail id="…">` could forge
the end of its own block and open a fake one — e.g. impersonating a different message
id right next to fabricated instructions that would read as real framing to the model.
Same issue for the literal string `[truncated]`, which could impersonate the marker the
code itself appends on truncation. Both are now neutralized (case-insensitive, in
subject/from/body only — never in the code-controlled id or date) by swapping the
ASCII brackets for fullwidth lookalikes before rendering, so the substrings a model
would parse as tag/marker syntax never appear in untrusted text. Two new tests: a body
containing a forged `</mail><mail id="x">` still produces exactly one real opening and
closing tag for the message; a body containing the literal `[truncated]` string never
collides with the real marker.

## Stage 7 — digest + feedback

**Done** (code + fixture/fake-model tests; the real Gmail insert needs NEED-MARCEL
item 1, `mail auth insert`).

- `src/mail/digest.py`: numbering, the `#n -> message_id` map (`digests.refs_json`),
  and every section except the two narrative ones are assembled by code, never by the
  model — the model (Sonnet) only receives pre-bucketed, pre-numbered items and
  returns one prose line per item, in the same order and count it was given. A
  mismatched line count is treated as a model failure, same as a `ClaudeCliError`:
  falls back to a mechanical line built straight from the tag's `summary`/`action`/
  `deadline`, with a `_Degraded_` marker at the top of the file. If there are no
  tagged messages at all since the last digest (or the last 24h with no prior digest),
  falls back further to a flat "New mail" list of raw subjects — the file always gets
  written either way.
- Sections: **Needs you** (`category=needs-you` and `importance>=2`, capped at
  `config.max_needs_you`), **Worth knowing** (`fyi`, capped at 15), **Receipts &
  notifications** (listed, numbered), **Newsletters** / **Junk** (counts only, per the
  plan — not numbered, so `refs_json` only ever maps to items actually shown).
  Low-importance `needs-you` falls through to Junk rather than disappearing silently.
- Gmail insert (`gmail.insert_raw_message`, new in `gmail.py`, uses the *insert*-scoped
  token/service — distinct from the readonly one everything else uses): `From:
  "life-agent" <config.address>`, `To: config.address` — both constants from code, the
  model never produces a recipient; `Message-ID:
  <digest-YYYYMMDD-{16 hex}@life-agent>`; `labelIds=[INBOX, UNREAD]`. A failed insert
  does not fail the run — the brief file already exists by that point, which is what
  the dead-man's switch actually checks (`failure-modes.md`'s "digest insert failing"
  row) — it's logged to stderr and recorded as `insert_error` instead.
- **Append-only in spirit**: `digest()` refuses to overwrite an existing
  `briefs/YYYY-MM-DD-mail.md` — re-running for a date that already has a file is a
  no-op (`skipped_existing`), not a silent rewrite.
- `src/mail/feedback.py`: finds owner replies whose `In-Reply-To`/`References` names a
  `digests.message_id_hdr` this archive actually sent, then parses `#n
  junk|important|fyi|needs-you|receipt`, `vip <addr|domain>`, `mute <addr|domain>`,
  `topic <words>`, and `reply #n: <text>` (stored only, v1 doesn't draft). Every parsed
  item is written to the `feedback`/`rules` tables *and* appended to
  `$LIFE_AGENT_DATA/mail-feedback.jsonl`. Runs at the end of every `mail tag` (also
  callable standalone as `mail feedback`).
- `tag.py`'s `is_muted`/`is_vip` now also consult the `rules` table (feedback-learned,
  in addition to `config.yaml`'s static lists) — a `vip`/`mute` rule value is matched as
  an address if it contains `@`, else as a domain.
- A `reply #n: …` feedback item stays "unacknowledged" until the next digest mentions
  it once ("Noted N reply request(s) … drafting isn't implemented yet (v2)") and marks
  it acknowledged — a small schema migration (`feedback.acknowledged_at`, v2) backs
  this.
- `mail digest [--date D] [--dry-run]` and `mail feedback` wired into `cli.py`.

**Verified:**
- `uv run pytest` — 149 passed. New: 14 `digest.py` tests (bucketing, capping,
  ref-numbering excludes count-only sections, the no-tags subjects-only fallback, model
  failure and line-count-mismatch degradation, the pending-reply acknowledgment, file
  writing + row recording, dry-run writes nothing, refusing to overwrite an existing
  brief, insert success/failure paths, the constant-recipient email-building test); 15
  `feedback.py` tests (every line format including case-insensitivity, an unknown `#n`
  being dropped, multiple lines in one reply, non-owner/unrelated replies ignored,
  storage + JSONL round trip); 4 CLI tests (`--dry-run` writes nothing, a normal run
  writes the file and reports `NEED-MARCEL` for the missing insert token without
  failing, `mail feedback` standalone).
- `bin/mail digest --dry-run` and `bin/mail feedback` on the real host (empty archive,
  no insert token yet) — both run cleanly against today's real date, exit 0, no crash.

**NEED-MARCEL:** `mail auth insert` (item 1's OAuth client + item 3's auth flow, insert
scope) is what's blocking a real Gmail insert — already flagged, still open. Once it
exists, the next `mail digest` run on real tagged mail is the first true end-to-end
check of stages 6+7 together.

## Stage 7b — query socket

**Done.**

- `src/mail/serve.py`: `MailQueryServer` — stdlib `http.server.BaseHTTPRequestHandler`
  over a `socketserver.UnixStreamServer`, no framework. Three GET-only endpoints:
  `/status`, `/search?q=&mode=&from=&since=&until=&limit=`, `/show?id=`. Every
  non-GET method (POST/PUT/DELETE/PATCH/HEAD) gets a 405; an unknown path gets a 404;
  `/search` and `/show` both require a query param (`q`/`id`) — there is no way to ask
  either endpoint for "everything," and `limit` is silently capped at 50 server-side
  regardless of what's requested. Opens the db `mode=ro`; loads `sqlite-vec` so
  `--mode vec/hybrid` works over the socket too (the embedder for that is built lazily,
  once, and cached on the server instance — not reloaded per request). Removes and
  recreates the socket file on startup (a stale one from an unclean shutdown would
  otherwise block binding), and chmods it `0660`.
- `src/mail/socket_client.py`: a plain-stdlib HTTP-over-Unix-socket client (raw
  request/response parsing — no `http.client` UDS support, no extra dependency for a
  handful of GET requests). `request()` returns `(status, payload)` without raising;
  `get()` wraps it and raises `SocketQueryError` on 4xx/5xx, which is what `cli.py`
  actually uses.
- `cli.py`: `mail serve [--socket PATH]`, and `mail status`/`search`/`show` now try
  `store.connect()` first and fall back to querying the socket on
  `sqlite3.OperationalError` — the same three commands work for both principals, per
  the plan, with no separate "socket mode" flag for the owner to remember.

**Verified:**
- `uv run pytest` — 169 passed. New: 16 `serve.py` tests (socket mode is `0660`, each
  endpoint's happy path, missing-param errors, unknown id is 404, unknown path is 404,
  every non-GET method parametrized and refused, limit silently capped, no endpoint can
  list the whole archive); 4 CLI fallback tests. The fallback tests needed a specific
  trick: a single test process can't reproduce "unreadable to the owner, readable to
  `life-agent`" with `chmod` alone, since both principals are the same OS user in this
  environment — so the tests run a *real* `MailQueryServer` against a fully-readable
  db (exactly like production, where the server genuinely can read it) while only
  `cli.py`'s own `store.connect` call is forced to raise, simulating the CLI's view of
  a locked-down db without touching what the server can see.
- `bin/mail serve --socket <tmp>` on the real host, queried via `socket_client.get` from
  a separate process — `/status` returns `{"messages": 0}` correctly.

**Left for stage 8:** the socket currently defaults to `/run/life-agent/mail.sock`,
which doesn't exist as a directory yet on this host (bootstrap creates it via
`tmpfiles.d`) — `mail serve` has only been run against an explicit `--socket` path so
far. Group ownership (`life-agent`) is likewise a bootstrap/systemd-unit concern, not
something `serve.py` sets itself beyond the `0660` mode.
