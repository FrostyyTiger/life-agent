"""The raw-message archive: every fetched message's original RFC 822 bytes, gzipped,
at `$LIFE_AGENT_STATE/raw/<YYYY>/<MM>/<gmail-id>.eml.gz` (month of Gmail's
`internalDate`, UTC). See docs/plans/mail-personal-v1.md stage 1.

`mail.db` holds what `extract.py` pulled out of a message; this holds the message
itself, so nothing extraction drops (attachments, every header, the exact MIME
structure) is lost. Files are written once and never deleted or rewritten: a message
deleted in Gmail keeps its file, the same way its row keeps living with `deleted_at`
set. The archive is a copy, not a mirror.

Local only — nothing in this module talks to the network (docs/egress.md).
"""

from __future__ import annotations

import gzip
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SUFFIX = ".eml.gz"
DIR_MODE = 0o700
FILE_MODE = 0o600

# Gmail ids are short hex strings. Anything else must never become a path component.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def default_raw_dir(conn) -> Path:
    """`raw/` next to the open `mail.db`, i.e. `$LIFE_AGENT_STATE/raw`."""
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not db_file:
        raise ValueError("raw archive needs a file-backed mail.db, not an in-memory one")
    return Path(db_file).parent / "raw"


def raw_path(raw_dir: Path, message_id: str, internal_date_ms: int) -> Path:
    if not _ID_RE.match(message_id):
        raise ValueError(f"refusing unsafe message id for a file name: {message_id!r}")
    when = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)
    return raw_dir / f"{when:%Y}" / f"{when:%m}" / f"{message_id}{SUFFIX}"


def _ensure_dirs(raw_dir: Path, leaf: Path) -> None:
    """Creates `leaf` and every directory between it and `raw_dir`'s parent at 0700,
    regardless of umask. Existing directories are left alone.
    """
    to_create = []
    current = leaf
    while not current.exists():
        to_create.append(current)
        if current == raw_dir:
            break
        current = current.parent
    for directory in reversed(to_create):
        try:
            directory.mkdir(mode=DIR_MODE)
        except FileExistsError:
            continue
        directory.chmod(DIR_MODE)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_raw(path: Path) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def write_raw(raw_dir: Path, message_id: str, internal_date_ms: int, raw_bytes: bytes) -> bool:
    """Stores one message's raw bytes. Returns True if a file was written, False if the
    same bytes were already there.

    Atomic and no-clobber: the bytes go to a temp file in the target directory, which is
    then hard-linked into place, so a reader never sees a half-written file and a
    concurrent writer can't replace an existing one. If a file with *different* bytes
    already exists, it is kept untouched (the archive never rewrites) and a warning
    goes to stderr.
    """
    final = raw_path(raw_dir, message_id, internal_date_ms)
    if final.exists():
        return _check_existing(final, raw_bytes)

    _ensure_dirs(raw_dir, final.parent)
    fd, tmp_name = tempfile.mkstemp(dir=final.parent, prefix=f".{message_id}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            # mtime=0 so the same bytes always compress to the same file.
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            return _check_existing(final, raw_bytes)
        _fsync_dir(final.parent)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _check_existing(path: Path, raw_bytes: bytes) -> bool:
    try:
        same = read_raw(path) == raw_bytes
    except (OSError, EOFError, gzip.BadGzipFile):
        same = False
    if not same:
        print(f"mail: {path.name} already archived with different bytes; keeping the "
              "existing file", file=sys.stderr)
    return False


def list_raw_ids(raw_dir: Path) -> dict[str, Path]:
    """Every archived message id -> its file. Temp files are not counted."""
    found: dict[str, Path] = {}
    if not raw_dir.is_dir():
        return found
    for path in raw_dir.glob(f"*/*/*{SUFFIX}"):
        if path.name.startswith("."):
            continue
        found[path.name[: -len(SUFFIX)]] = path
    return found


def tree_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    if root.is_file():
        return root.stat().st_size
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
