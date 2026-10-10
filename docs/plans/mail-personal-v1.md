# mail-personal-v1: the mail archive goes live in its own GPU container

**Status:** plan, 2026-10-10. Stages 1–2 in progress. Revised the same day: the host is a dedicated LXC with a shared GPU, not the CPU-only VM (D1).
**Builds on:** `docs/plans/mail-v1.md` (stages 1–10 done, never run against real mail). Its original host
was retired before the owner items landed, so nothing ever synced.
**Host specifics** (hostname, paths, access) are in the private notes repo, `mail-personal-kickoff.md`.
This file names no real host, path or address.

## Goal

1. **A copy of every mail.** Every Gmail message lands on the host as its original RFC 822 bytes, plus the
   parsed row in `mail.db` that mail-v1 already writes. The copy is complete: one command proves
   Gmail's message count, the DB rows and the raw files all agree.
2. **Embeddings next to the copy.** Embedding is its own job on its own timer, separate from the
   15-minute sync, so a slow or failed embed run never holds up the copy.
3. **Search.** Use the existing hybrid search (FTS5 + `bge-m3` vectors) through the read-only socket and
   `mail mcp`, unchanged.

## Out of scope (stays off)

Tagging (`mail tag`), the digest (`mail digest`), `gmail.insert`, `claude -p`, the `publish`/`brief`/`deadman`
timers, reply drafting. The code stays. The timers are not enabled and no Claude token or insert token is
minted. They come back in a later plan, once the archive has run cleanly for a while.

## The host, as it differs from mail-v1's assumptions

| mail-v1 assumed | this host |
|---|---|
| a full GPU host | a **dedicated unprivileged LXC** that shares the homelab's one consumer GPU with other trusted containers (D1) |
| embedding inside `sync --budget 600` | embedding is a separate, low-priority timer (stage 2) |
| `$STATE_DIR` under the owner's home | `$STATE_DIR=/var/lib/life-agent/state`, on the container's own disk (D3) |
| the owner's home dir is a login account | the container holds only this project; the owner logs in to install and to search |

The trust model does not change: the `life-agent` user, a `0700` state directory, `0600` tokens, the
read-only socket as the only way in for other sessions, and `gmail.readonly` as the only Gmail scope used.

## Stages

Code stages (1–2) touch only the public repo and its fixtures, with no real mail involved. They can run as a lane on
the agent VM. Host stages (3–5) need sudo on the target host and real credentials, so they are done
interactively over SSH, with the owner present for the consent step.

### Stage 1: keep the raw message (code)

`gmail.py` already fetches `format="raw"` but only stores the extracted fields, so the original bytes are
thrown away. Keep them.

- In `_store_message`, write `raw_message["raw"]` gzip-compressed to
  `$STATE_DIR/raw/<YYYY>/<MM>/<gmail-id>.eml.gz` (month from `internal_date`, UTC). Write to a temp file and
  rename it into place. Mode `0600`, directories `0700`. If the file already exists with the same
  bytes, do nothing.
- Never delete a raw file. A message deleted in Gmail sets `deleted_at` on the row, as today, and its raw
  file stays. The archive is a copy, not a mirror.
- New `mail verify` command: Gmail `messagesTotal` (from the profile) vs. live rows vs. raw files. It also
  lists rows without a raw file and files without a row. It exits non-zero on any gap. `mail status`
  prints the same three counts.
- New `mail backfill-raw`: for rows that have no raw file, refetch the raw message by id. This repairs the
  archive and is not part of normal sync.
- Tests (fixtures only): the file is written and gunzips to the exact bytes, re-sync leaves it alone,
  deletion keeps it, `verify` catches a missing file and an orphan file, and the leak test still passes.

### Stage 2: embedding as its own job (code)

- `sync` gets `--no-embed`. The installed sync unit uses it, while the default stays as it is for GPU hosts.
- New units `life-agent-mail-embed.service` + `.timer`: hourly (`OnCalendar=hourly`, `Persistent=true`),
  `ExecStart=… mail embed --budget 2700`. Settings:
  - `Nice=19`, `IOSchedulingClass=idle`, `CPUWeight=20`, `MemoryMax=4G`. The GPU is shared, so the
    embed run and the query service each hold `bge-m3` in fp16 (~1.2 GB VRAM each).
  - The same hardening as the sync unit. Keep `MemoryDenyWriteExecute` off: torch's CPU path uses oneDNN,
    which generates kernels at runtime.
  - The embed run and the sync run must not corrupt each other. SQLite WAL plus a busy timeout covers
    this. Test a sync running in parallel with an embed batch.
- `embed.py`: batch chunks across messages (e.g. 32 chunks per `encode`) instead of one message per call.
  On the CPU fallback, set `torch.set_num_threads(os.cpu_count())`. Keep one commit per message, so a run can still
  be interrupted and resumed.
- `mail embed --measure N`: embed N recent messages into a scratch table and print messages/min, peak RSS
  and peak VRAM. Nothing is committed. It sizes the backfill and checks that the GPU is shared without trouble.
- Tests: `--no-embed` skips the embedder, the batched path writes the same chunks/vectors as the
  per-message path (stub embedder), and the budget cut-off works mid-batch.

### Stage 3: the container, then the install (host, interactive)

0. Homelab side (root on the hypervisor, done from the machine that holds that key): create the unprivileged LXC
   following the homelab's existing GPU-room pattern (device nodes, the identical NVIDIA userspace
   `--no-kernel-module`, IPv6 off if Hugging Face downloads hang). Then join the tailnet tagged, add it to the
   nightly backup, and check that `nvidia-smi` and `torch.cuda.is_available()` work inside it. The homelab
   repo's room doc and GPU doc list it as a GPU room (one more userspace to upgrade in lockstep).
1. Install `uv`. Clone this repo and the private data repo into the owner's assistant directory. The data
   repo only needs `config.yaml`, `threads/` and `briefs/` to exist; the clone can be read-only.
2. `uv sync`. Then download `BAAI/bge-m3` into `$STATE_DIR/models` once, as `life-agent`.
3. `setup/bootstrap.sh` with `STATE_DIR=/var/lib/life-agent/state` and the other dirs overridden. Dry run
   first, read the dry-run output, then `--apply`. Warnings about a missing Claude token or insert token are
   expected and fine.
4. Template the units (sed loop in `setup/README.md`). Enable **only** `life-agent-mail-sync.timer` (with
   `--no-embed`), `life-agent-mail-embed.timer` and `life-agent-mail-query.service`.
5. Run every boundary check that bootstrap prints. Each is a claim in `docs/trust-model.md`.

### Stage 4: credentials (owner)

1. Google Cloud: check whether the OAuth desktop client from mail-v1 still exists and reuse it, or create one.
   Scope `gmail.readonly` only for now. The consent screen must be **Production**, because Testing tokens
   expire after 7 days. The client goes to `$CONF_DIR/google-client.json`.
2. Consent run: tunnel the callback port (`ssh -L 8765:localhost:8765 <host>`), run `mail auth readonly` per
   `setup/README.md`, then re-run bootstrap step 6 so the token becomes `life-agent:life-agent 0600`.
3. Fill in `mail.address` in the data repo's `config.yaml` if it is still the example value.

### Stage 5: first run, backfill, proof (host)

1. Smoke test: `mail sync --budget 60 --no-embed` as `life-agent`, then `mail status`.
2. Let the timers backfill. The fetch is fast (Gmail quota is no issue at this scale); embedding is the long
   pole. Record start time, `messagesTotal`, and the stage-2 `--measure` rate.
3. When the fetch is done, `mail verify` must exit 0. It is the proof that every mail is copied.
4. Check search from another session through the socket: `mail search --mode fts` right away, and `hybrid`
   once embeddings cover recent mail (embedding runs newest 30 days first).
5. Backups: confirm the next nightly backup includes `$STATE_DIR`, and do one restore test of a single
   `.eml.gz` file plus `mail.db` opened read-only.
6. Register `mail mcp` for Claude Code: on the container itself, and on the owner's laptops as an
   ssh-wrapped stdio server (`ssh <container> <code>/bin/mail mcp`). The search runs in the container; only the
   results travel back.
7. Docs: `docs/status/mail-personal-v1.md` here, and the host's room doc in the homelab repo.

## Decisions

- **D1, where embedding runs: DECIDED 2026-10-10 (owner).** A dedicated unprivileged LXC with the shared GPU.
  The model stays `bge-m3`. Rejected: the CPU-only VM (slow backfill, and its RAM could not hold the embed run
  and the query service at once; a VM cannot share the GPU). Also rejected: keeping the archive on the VM and
  sending text to a GPU room's embed server (mail text would pass through a second room that runs CI or
  holds client data). Cost accepted: one more container that shares the host's GPU driver, which is the most
  plausible container-escape surface. Acceptable because the room is trusted and not internet-facing beyond
  outbound HTTPS. CPU-only torch was dropped from stage 2. The CPU path stays as the fallback.
- **D2, cadence:** sync every 15 min (unchanged), embed hourly with a 45-min budget at idle priority.
- **D3, where the archive lives:** on the container's own disk under `/var/lib/life-agent/state`, which the
  nightly encrypted backup covers. Not on any shared host dataset.
- **D4, who runs what:** stages 1–2 as a lane on the agent VM (public repo, fixtures only), stages 3–5
  interactively on the host. No personal repo and no credential goes to the agent VM.

## Failure modes to cover (additions to `docs/failure-modes.md`)

- Embed falls behind (backlog grows): `mail status` shows the un-embedded count, and search degrades to FTS
  for those messages. No data loss.
- OOM while embedding: `MemoryMax` kills only the embed run, sync is unaffected, and the next hourly run
  resumes from the next un-embedded message.
- Token revoked or expired: sync prints `NEED-MARCEL:` and exits 2 (already built). The archive stays readable.
- GPU busy or gone (another room's job, a driver mismatch after a host upgrade): embed falls back to CPU
  with a loud warning and keeps going slowly. The copy is unaffected.
- Disk: raw `.eml.gz` + DB for a large mailbox is single-digit GB. Size the container disk at 40 GB or more.
  `mail status` prints the size.
