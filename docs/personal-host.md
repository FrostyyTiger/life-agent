# The personal host

Mail and the brief are two capabilities. The rest of a private life is files: photos, scanned
letters, old backups, study material. This document describes the host that holds all of it and the
conventions for an assistant working on it. It is a design, written down after building it once; the
host-specific details live in the private notes.

## Shape

One virtual machine on an always-on home server, reachable only over a private overlay network
(a tailnet). Two storage tiers with deliberately different jobs:

| Tier | Backing | Holds | Must survive |
| --- | --- | --- | --- |
| **Working disk** | the VM's own virtual disk, on fast local flash | the OS, containers, the document server, the file index and move log, working copies, the repo mirror, the browser profile | everything — nightly encrypted VM backup offsite |
| **Archive disk** | a removable USB hard disk, imported on the *host* as an encrypted ZFS pool and shared into the VM read-write (virtiofs) | raw files only: `Photos/`, `Images/`, `Study/`, later `Documents/` | being unplugged — and a disk failure, via snapshots + a nightly encrypted host-level backup |

Two rules make the split work:

- **Nothing that runs may need the archive disk.** With the disk unplugged the VM boots, every
  service starts, and `/data` is an empty, immutable directory. Anything that writes there checks
  `mountpoint -q /data` first. The network share refuses connections while the disk is absent.
- **The archive disk is owned by the host, not the VM.** The host imports, unlocks, snapshots and
  scrubs it and backs it up from a snapshot. The VM only sees a directory. A VM with the raw disk
  passed through would have to do all of that itself, and would take the backups down with it.

### Archive disk details

- Single disk for now; a second disk later is a `zpool attach`, i.e. a mirror without downtime.
  Both disks in one enclosure survive a dead disk, not a dead enclosure — the offsite backup covers that.
- ZFS native encryption, unlocked by a key file at boot (protects the disk when it leaves the house:
  theft, loss, disposal). A copy of the key lives off the machine, in the owner's password manager.
- `cachefile=none` + a oneshot unit that imports it if present: a missing disk is a log line, not a
  failed boot. `failmode=continue`: a pulled cable is an I/O error, not a frozen pool.
- `utf8only` + `normalization=formD`: what the macOS SMB client expects. CMR, not SMR, disks.
- Snapshots hourly (keep 24), daily (30), monthly (12); a scrub monthly; all skip when the disk is absent.
- The nightly host backup reads `personal.pxar` from a fresh snapshot and **skips with a mail when the
  pool is absent**, so an empty directory is never backed up as if it were the data.

### Access from the owner's devices

A Samba share of `/data` inside the VM, SMB3 only, `hosts allow` limited to the tailnet's address
ranges (the home LAN is refused), Apple `fruit` metadata via `streams_xattr`. Desktop, laptop and phone
mount it like a NAS. A second share is the document server's drop folder.

## Modules

### Files and photos — live

The assistant here is an interactive Claude Code session, not the unattended `life-agent` user.
The workflow is built so that the model never moves a file it has not been told to:

1. **Index, by script.** Path, size, mtime, sha256, and for media the EXIF date, camera, GPS and
   dimensions (`exiftool`). Deterministic, local, minutes for tens of thousands of files. No model:
   a script is exact and free; a model is only worth it for judgement.
2. **Propose, by the model, from metadata.** Duplicates (by hash), RAW/JPEG pairs, undated files,
   screenshots vs camera photos, a target layout. Shown to the owner as counts and examples.
3. **Approve, by the owner.** Answers to a handful of questions, not a review of every file.
4. **Move, by script.** A plan file (`src → dst → reason`) generated first, sanity-checked against
   the index, then executed as atomic renames on one filesystem (no data copied), never overwriting.
   Every move is appended to an append-only `moves.log` on the working disk.

Guard rails, in order of how much they are relied on:

| Guard | What it prevents | Enforced by |
| --- | --- | --- |
| `@pre-<session>` snapshot before every session | any mistake in the session | ZFS (owner/host); the session cannot destroy snapshots from inside the VM |
| Never delete: duplicates and junk go to `_trash/`, emptied only by the owner | silent data loss | convention |
| `moves.log`, append-only | "where did that file go?" | convention + the log living outside the archive disk |
| Plan file before moving | a bad rule touching unintended files | convention |

The resulting photo layout: `Photos/<YYYY>/<YYYY-MM>[ <event>]/` with camera RAW files in a `RAW/`
subfolder next to their JPEGs, so the folder stays browsable on a phone. Images that are not the
owner's photos (screenshots, saved images) go to `Images/`.

### Documents — live

[Paperless-ngx](https://docs.paperless-ngx.com/) on the working disk (not the archive disk — it must
run with the disk unplugged): OCR, full-text search, correspondents, types, tags. Scans arrive from
a phone app or the drop share. HTTPS on the tailnet only; the web port is bound to localhost.

- **Backups:** a daily `document_exporter` run to a directory on the working disk, picked up by the
  nightly VM backup. The VM image alone is crash-consistent; the export is the clean, version-portable copy.
- **The assistant's access:** its own Paperless user with an API token stored in a `0600` file on the
  VM (never in a transcript), and **no permission on documents tagged `private`**. This is the one
  privacy boundary in this document that is enforced by software rather than by convention.

### Planned

- **Repo mirror.** A nightly `git clone --mirror` / `remote update` of the owner's repositories with a
  fine-grained, read-only token. Read-only by construction: it cannot push. Older states survive
  force-pushes in the VM backup's history.
- **A browser for sites without an API.** A persistent Chromium on the VM. The owner logs in once
  through a web viewer on the tailnet (passwords and second factors never pass through the
  assistant); the assistant drives the same browser over a CDP port bound to localhost. Payments
  stay out of scope.
- **Photo library.** A self-hosted photo service reading `Photos/` as an external library, with
  face and place search running on a local GPU.

## Privacy: what reaches the model provider

Interactive sessions send whatever they read to the model API, and keep it in local transcripts.
The working rule is **metadata first, content only when needed, one item at a time**:

- By default the session sees names, paths, sizes, dates, hashes and EXIF fields — enough for
  duplicates, dating and structure. Contents are opened individually, when a decision needs them.
- A **no-look list** — identity documents, social-security numbers and anything carrying them, tax
  and health records, and files belonging to other people — is handled by name only. On the archive
  disk this is a **convention** (the session runs as the owner and could read them). In the document
  server it is **enforced** by the `private` tag permission above.
- Secrets (passwords, keys, tokens) never enter a session. They are typed by the owner into their own
  terminal and stored in files the session uses without printing.
- Nothing in this module adds an outbound path other than the model API and the encrypted offsite
  backups. The document server, the index and the move scripts make no network calls.

The unattended egress ledger is [`egress.md`](egress.md); this section covers the owner's interactive
sessions, which that ledger does not.
