---
name: files
description: Organise, find, deduplicate or clean up files and photos on the archive disk (/data). Use before moving, renaming or trashing anything there.
---

# Files and photos on the archive disk

`/data` is the removable encrypted archive disk, shared in from the host. It may be absent: check
`mountpoint -q /data` before every read or write job. The index and the move log live on the VM disk
in `~/files-index/` so they exist while the disk is unplugged.

## Layout

| Path | Holds |
| --- | --- |
| `Photos/<YYYY>/<YYYY-MM>[ <event>]/` | the owner's own photos and videos; camera RAW in a `RAW/` subfolder next to the JPEGs |
| `Images/Screenshots`, `Images/Saved` | images that are not the owner's photos |
| `Study/`, later `Documents/` | as the names say |
| `_inbox/<source>-<date>/` | new arrivals, one folder per source, untouched until sorted |
| `_trash/<date>-<topic>/` | anything to remove; only the owner empties it |

## Workflow: index, propose, approve, move

1. **Index (script).** `files-index <dir>` writes `~/files-index/photos.tsv`: path, size, sha256,
   EXIF date and source, camera, GPS, dimensions. Local, read-only on `/data`.
2. **Propose (you, from metadata).** Duplicates by hash, RAW/JPEG pairs, undated files,
   screenshots vs camera photos, a target layout. Show counts and a few examples, not file lists.
3. **Approve (owner).** A handful of questions; the answers become the rules.
4. **Plan, then move (script).** Write a plan TSV (`src<TAB>dst<TAB>reason`) with a small script for
   this session; sanity-check it against the index (no dst collisions, every src exists, counts match
   the proposal). Ask the host for a `@pre-<topic>-<date>` snapshot (or ask the owner to take one).
   Then `plan-move <plan.tsv>`: atomic renames on one filesystem, never overwrites, appends every
   move to `~/files-index/moves.log`.

Session-specific plan generators stay on the VM (`~/files-index/`); they contain personal paths and
are not committed anywhere public.

## Gotchas
- Folders copied from NTFS can arrive read-only: `chmod u+w` the directories (not files) before a move.
- Never use `rm`; use `plan-move` with a `_trash/` destination.
- Opening a file sends its content to the model provider. Stay on metadata unless the decision needs
  the content, and never open anything on the no-look list.
