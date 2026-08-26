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
