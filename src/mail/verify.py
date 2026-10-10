"""Proof that the archive is complete, and the repair for when it is not.

`archive_counts` is what `mail status` (and the query socket's /status) print.
`verify` is `mail verify`: Gmail vs. rows vs. raw files, exiting non-zero on any gap.
`backfill_raw` is `mail backfill-raw`: refetches the raw bytes for rows that have no
raw file (rows stored before stage 1 of docs/plans/mail-personal-v1.md existed, or a
file lost some other way). It is a repair, not part of normal sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from src.mail import rawstore

GMAIL_TOTAL_KEY = "gmail_messages_total"
GMAIL_TOTAL_AT_KEY = "gmail_messages_total_at"
BATCH_SIZE = 50


def _scalar(conn, sql: str, params=()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def count_unembedded(conn) -> int:
    return _scalar(
        conn,
        "SELECT COUNT(*) FROM messages m WHERE m.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_id = m.id)",
    )


def archive_counts(conn, raw_dir: Path, db_path: Path) -> dict:
    """Everything `mail status` reports about the archive, from local state only (no
    Gmail call: the Gmail total is the one the last sync recorded).
    """
    def state(key):
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    gmail_total = state(GMAIL_TOTAL_KEY)
    db_bytes = sum(
        rawstore.tree_size_bytes(Path(f"{db_path}{suffix}")) for suffix in ("", "-wal", "-shm")
    )
    return {
        "messages": _scalar(conn, "SELECT COUNT(*) FROM messages"),
        "rows_live": _scalar(conn, "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"),
        "rows_deleted": _scalar(conn, "SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL"),
        "raw_files": len(rawstore.list_raw_ids(raw_dir)),
        "gmail_total": int(gmail_total) if gmail_total is not None else None,
        "gmail_total_at": state(GMAIL_TOTAL_AT_KEY),
        "pending_fetch": _scalar(conn, "SELECT COUNT(*) FROM pending"),
        "unembedded": count_unembedded(conn),
        "disk_bytes": db_bytes + rawstore.tree_size_bytes(raw_dir),
    }


@dataclass
class VerifyReport:
    gmail_total: int | None
    gmail_listed: int
    rows_live: int
    rows_deleted: int
    raw_files: int
    pending: int
    backfill_complete: bool
    in_gmail_not_archived: list[str] = field(default_factory=list)
    archived_not_in_gmail: list[str] = field(default_factory=list)
    rows_without_raw: list[str] = field(default_factory=list)
    raw_without_row: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.backfill_complete
            and self.pending == 0
            and not self.in_gmail_not_archived
            and not self.rows_without_raw
            and not self.raw_without_row
        )


def verify(conn, port, raw_dir: Path) -> VerifyReport:
    """Compares three things and lists every difference:

    - Gmail: the profile's `messagesTotal`, and the exact set of ids
      `messages.list` returns (the same listing sync's backfill uses, so spam and
      trash are excluded on both sides).
    - `mail.db` rows: live (no `deleted_at`) and deleted.
    - raw files under `raw_dir`.

    The gate is the id set, not `messagesTotal`: Gmail's total counts every message
    in the mailbox, which may include spam and trash that sync never lists. A
    difference there is reported but does not fail verify on its own.
    """
    profile = port.get_profile()
    gmail_total = profile.get("messages_total")
    gmail_ids = set(port.list_all_message_ids())

    rows = conn.execute("SELECT id, deleted_at FROM messages").fetchall()
    all_row_ids = {row["id"] for row in rows}
    live_ids = {row["id"] for row in rows if row["deleted_at"] is None}
    raw_ids = set(rawstore.list_raw_ids(raw_dir))

    backfill_row = conn.execute(
        "SELECT value FROM sync_state WHERE key = 'backfill_complete'"
    ).fetchone()

    return VerifyReport(
        gmail_total=int(gmail_total) if gmail_total is not None else None,
        gmail_listed=len(gmail_ids),
        rows_live=len(live_ids),
        rows_deleted=len(all_row_ids) - len(live_ids),
        raw_files=len(raw_ids),
        pending=_scalar(conn, "SELECT COUNT(*) FROM pending"),
        backfill_complete=bool(backfill_row and backfill_row[0] == "1"),
        in_gmail_not_archived=sorted(gmail_ids - all_row_ids),
        archived_not_in_gmail=sorted(live_ids - gmail_ids),
        rows_without_raw=sorted(all_row_ids - raw_ids),
        raw_without_row=sorted(raw_ids - all_row_ids),
    )


def format_report(report: VerifyReport, max_ids: int = 20) -> str:
    def total(value):
        return "unknown" if value is None else str(value)

    lines = [
        "mail verify",
        f"  gmail messagesTotal:   {total(report.gmail_total)}",
        f"  gmail listed ids:      {report.gmail_listed}",
        f"  rows (live):           {report.rows_live}",
        f"  rows (deleted, kept):  {report.rows_deleted}",
        f"  raw files:             {report.raw_files}",
        f"  pending fetch:         {report.pending}",
    ]
    if not report.backfill_complete:
        lines.append("  GAP: the initial backfill has not finished")
    if report.pending:
        lines.append(f"  GAP: {report.pending} id(s) still queued for fetch")

    def listing(label, ids):
        if not ids:
            return
        lines.append(f"  {label}: {len(ids)}")
        for message_id in ids[:max_ids]:
            lines.append(f"    {message_id}")
        if len(ids) > max_ids:
            lines.append(f"    ... and {len(ids) - max_ids} more")

    listing("GAP: in Gmail, not archived", report.in_gmail_not_archived)
    listing("GAP: rows without a raw file (run `mail backfill-raw`)", report.rows_without_raw)
    listing("GAP: raw files without a row", report.raw_without_row)
    listing("note: live rows Gmail no longer lists (trash/spam, or deleted)",
            report.archived_not_in_gmail)
    if report.gmail_total is not None and report.gmail_total != report.gmail_listed:
        lines.append(
            f"  note: messagesTotal ({report.gmail_total}) != listed ids "
            f"({report.gmail_listed}); messagesTotal also counts spam/trash"
        )
    lines.append("  OK: every listed Gmail message has a row and a raw file"
                 if report.ok else "  FAILED: see GAP lines above")
    return "\n".join(lines)


@dataclass(frozen=True)
class BackfillRawResult:
    written: int
    unrecoverable: list[str]
    remaining: int


def backfill_raw(conn, port, raw_dir: Path, budget_seconds: float | None = None
                 ) -> BackfillRawResult:
    """Refetches the raw message for every row (live or deleted) without a raw file.
    A message Gmail no longer serves cannot be repaired; it is reported, never
    retried in a loop.
    """
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    raw_ids = set(rawstore.list_raw_ids(raw_dir))
    missing = [
        row["id"] for row in conn.execute("SELECT id FROM messages ORDER BY internal_date DESC")
        if row["id"] not in raw_ids
    ]

    written = 0
    unrecoverable: list[str] = []
    done = 0
    for start in range(0, len(missing), BATCH_SIZE):
        if deadline is not None and time.monotonic() >= deadline:
            break
        ids = missing[start : start + BATCH_SIZE]
        fetched = port.get_messages(ids)
        for message_id in ids:
            raw = fetched.get(message_id)
            if raw is None:
                unrecoverable.append(message_id)
                continue
            if rawstore.write_raw(raw_dir, message_id, raw["internal_date"], raw["raw"]):
                written += 1
        done += len(ids)

    return BackfillRawResult(
        written=written, unrecoverable=unrecoverable, remaining=len(missing) - done
    )
