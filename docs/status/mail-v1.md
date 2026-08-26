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

**Left for stage 2:** the actual schema (`messages`, `messages_fts`, `chunks`,
`vec_chunks`, `tags`, `feedback`, `rules`, `digests`, `sync_state`, `pending`) and
`store.py`'s insert/upsert/migration logic. `LIFE_AGENT_STATE` does not exist as a
directory on disk yet — stage 2 or 3 will create it (currently only referenced via env
var; nothing has written into it).
