# mail-personal-v1 status

What was built for [`../plans/mail-personal-v1.md`](../plans/mail-personal-v1.md), how it was
verified, and what the host stages still need. Stages 1 and 2 (code, fixtures only) are done;
stages 3 to 5 (container, credentials, real mail) have not started.

The digest, tagging, `gmail.insert` and `claude -p` are unchanged and stay off. Their code and
unit templates are still in the repo; the archive install simply does not enable them.

## Stage 1: keep the raw message

**Done.**

- `src/mail/rawstore.py`: `_store_message` (in `gmail.py`) writes every fetched message's
  original RFC 822 bytes, gzipped, to `$LIFE_AGENT_STATE/raw/<YYYY>/<MM>/<gmail-id>.eml.gz`
  (UTC month of `internalDate`) **before** it upserts the row. A crash between the two leaves
  an archived file whose id is still in `pending`, so the next run stores the row; the other
  order could leave a row with no copy of the message behind it.
  - Atomic and no-clobber: a temp file in the target directory, fsynced, then hard-linked into
    place, so a half-written file is never visible and a concurrent writer cannot replace one.
  - Files `0600`, directories `0700`, set explicitly, whatever the umask.
  - Same bytes again: nothing happens (`gzip` with `mtime=0`, compared after decompressing).
    Different bytes for an id already archived: the existing file is kept, with a warning on
    stderr. Nothing in the code deletes or rewrites a raw file. A Gmail deletion still only
    sets `deleted_at`.
  - Ids are checked against `[A-Za-z0-9_-]` before they become file names.
  - The raw dir defaults to `raw/` next to `mail.db`; `mail sync` passes
    `$LIFE_AGENT_STATE/raw` explicitly.
- `mail verify` (`src/mail/verify.py`): compares Gmail (the profile's `messagesTotal` and the
  exact id set from `messages.list`), rows (live and deleted) and raw files. It lists ids that
  are in Gmail but not archived, rows without a raw file and raw files without a row. It exits
  1 on any of those, while the initial backfill is unfinished, or while ids are still queued.
  Exit 2 with `NEED-MARCEL:` if the readonly token is missing.
  - **Deviation from the plan:** the gate is the listed id set, not `messagesTotal`. Gmail's
    total may also count spam and trash, which sync never lists (`includeSpamTrash=False`),
    so gating on it could fail forever on a complete archive. The total is still printed, and
    a difference shows up as a `note:` line. Check on the host in stage 5 whether the two
    agree; if they do, the gate can be tightened.
  - Live rows that Gmail no longer lists (trashed, or deleted with no history event seen) are
    printed as a note, not a gap: the archive is a copy, not a mirror.
- `mail backfill-raw`: refetches the raw message for every row (live or deleted) without a
  raw file, 50 per batch, with an optional `--budget`. A message Gmail no longer serves is
  reported as unrecoverable; its row stays. Exit 1 if anything is left missing.
- `mail status` (and the query socket's `/status`, so the owner sees it without reading the
  DB) prints the Gmail total recorded by the last sync, live/deleted rows, raw files, pending
  fetch, the un-embedded count and the size of `mail.db` + `raw/`. Every sync run now makes
  one `getProfile` call to record the total.
- Docs: `docs/egress.md` lists `raw/` as local-only storage and the on-demand reads of
  `verify`/`backfill-raw` under row 5 (same scope, same endpoint); `docs/trust-model.md`'s
  boundary diagram lists `raw/` inside the `0700` state dir.

## Stage 2: embedding as its own job

**Done**, except the CPU-only torch install path, which was dropped (see D1 below).

- `mail sync --no-embed` fetches only and never loads the model. Without the flag, sync embeds
  afterwards as before.
- `setup/systemd/life-agent-mail-embed.{service,timer}`: hourly, `Persistent=true`,
  `embed --budget 2700`, `Nice=19`, `IOSchedulingClass=idle`, `CPUWeight=20`,
  `MemoryMax=4G`, the sync unit's hardening (a test compares the two), no
  `MemoryDenyWriteExecute` (CUDA, and oneDNN on the CPU fallback). The sync unit template now
  runs `sync --budget 600 --no-embed`.
- `store.connect` sets a 60 s busy timeout on top of WAL, so the two units can write at the
  same time.
- `embed.py`: chunks from several messages go to the model together (about 32 per call; a
  message's chunks are never split across calls). Writes stay one message per commit. The
  budget is checked before each batch, and a batch in flight is finished and committed. A
  message that gained chunks meanwhile (two runs in parallel) is skipped, not duplicated. Only
  ids are listed up front; rows load per batch, so a large backlog is not held in memory. On
  the CPU fallback torch uses every CPU the process may run on.
- `mail embed --measure N`: embeds the N newest messages into a TEMP table and commits
  nothing. Prints messages/min, the backlog and its projected hours, RSS and VRAM with the
  model loaded (which is what the query service keeps resident after its first `vec`/`hybrid`
  search), peak RSS, and peak VRAM (`torch.cuda.max_memory_allocated`, when CUDA is there).
- `docs/failure-modes.md`: rows 19 to 24 (embed backlog, embed OOM, concurrent writers, a
  full shared GPU, archive gaps, disk), plus updates to rows 13 and 16.
- `setup/README.md`: an "Archive only" install with exactly three units (sync, embed, query).
  `bootstrap.sh`'s closing hint now names that set.

### D1, decided during the lane

The host is a dedicated unprivileged LXC with the shared GPU, not the CPU-only VM. So:

- No CPU-only torch path (task T5 dropped). `uv sync` installs the normal wheels. The CPU path
  in `embed.py` stays as the fallback.
- The embed run and the query service each hold `bge-m3` fp16 on the GPU, about 1.2 GB VRAM
  each. The embed unit's `MemoryMax` is 4G.

## How it was verified

- `uv run pytest`: **229 passed, 1 skipped** (the skip is `test_no_leaks.py`, which needs the
  private config, so it only runs where that exists). Baseline before the lane: 188 + 1 skipped,
  after a test-only fix for a git-identity test that failed on any machine with a fully
  qualified hostname.
- New tests, fixtures and the stub embedder only:
  - `test_rawstore.py`: exact gunzipped bytes, `0600`/`0700` under a `022` umask, no temp
    files left, no-op on the same bytes, never overwritten by different bytes, re-sync
    (`--full`) leaves the inode and mtime alone, a Gmail deletion keeps the file, raw is written
    before the row, unsafe ids are refused.
  - `test_verify.py`: OK after a complete sync, catches a missing file, an orphan file, a
    message not yet archived and an unfinished backfill; a deleted message still verifies;
    `messagesTotal` mismatch is a note; `backfill-raw` repairs, reports unrecoverable ids and
    keeps to its budget; CLI exit codes; `mail status` lines.
  - `test_concurrency.py`: a writer waits for a held write lock instead of failing; sync (300
    messages) and repeated embed runs in parallel threads on separate connections. Checks:
    `integrity_check` ok, every message stored, archived and embedded exactly once, and
    embedding overlapped the sync.
  - `test_embed.py`: batched and per-message paths produce identical chunks and vectors with
    fewer model calls; a budget that runs out mid-batch commits that batch and the next run
    resumes; no duplicates; `measure` commits nothing.
  - `test_cli.py`: `--no-embed` never builds the embedder; without it sync still embeds; the
    unit templates have the stage-2 settings and identical hardening.
- One real-model smoke run on the agent VM, CPU only, synthetic fixtures in a scratch
  directory: `mail embed --measure 12` loaded `BAAI/bge-m3` on 6 threads, embedded the 12
  fixtures in 3 s, model-loaded RSS about 2.4 GB, peak RSS about 2.8 GB. `mail embed` then
  `mail search --mode hybrid` and `mail status` worked end to end. The fixtures are short,
  so this rate says nothing about real mail; on the host, `--measure` does.
- The archive-only `sed` loop from `setup/README.md` was run against the five templates: no
  placeholder left, and `systemd-analyze verify` complains only that the made-up python path
  does not exist.
- Not verified here: anything against real Gmail (`RealGmailPort.get_profile`'s
  `messagesTotal`, `verify`'s listing on a real mailbox, `backfill-raw`), CUDA, and the units
  under real systemd. Those are stages 3 to 5.

## What stages 3 to 5 need

Per the plan, interactively on the host, owner present:

1. Stage 3: create the LXC (GPU device nodes, matching NVIDIA userspace); check `nvidia-smi`
   and `torch.cuda.is_available()`; `uv sync`; download the model as `life-agent`; bootstrap
   with `STATE_DIR=/var/lib/life-agent/state` (dry run first); install only sync, embed and
   query per `setup/README.md` "Archive only"; run every boundary check bootstrap prints.
2. Stage 4: the OAuth desktop client (`gmail.readonly` only, consent screen in Production),
   `mail auth readonly` through the tunnelled callback, bootstrap step 6 again for the token's
   owner and mode, and `mail.address` in the data repo's config.
3. Stage 5: `mail sync --budget 60 --no-embed`, `mail status`, `mail embed --measure 50`
   (record the rate and peak VRAM with the query service also loaded), let the timers
   backfill, then **`mail verify` must exit 0**. Check whether `messagesTotal` equals the
   listed id count (see the deviation above). Then search through the socket, a backup and
   restore test of one `.eml.gz` plus `mail.db`, and the `mail mcp` registration.

Open points for the owner:

- `verify`'s gate (the id set rather than `messagesTotal`), above.
- On a full GPU (another room's job), embedding fails and retries next hour; it does not
  fall back to CPU automatically. The plan's failure list says it falls back. That is true
  for a missing GPU (no CUDA, or a driver mismatch), not for a full one. Changing that would
  mean touching the fallback in `embed.py`, which this lane was told to leave alone.
