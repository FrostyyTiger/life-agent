"""A fake implementing gmail.py's GmailPort interface, serving examples/mail/*.eml.

Models just enough of Gmail's history/backfill semantics for sync() to be exercised
end-to-end: an initial mailbox state (seed), messages arriving or disappearing after
that (deliver/delete, each advancing a history id), and simulated history-retention
expiry (expire_history_before). It does not touch googleapiclient/HTTP at all — sync()
is written against this interface, not against Google's client machinery, so faking
that machinery would test nothing sync() actually depends on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.mail import gmail


def raw_message(
    path: Path,
    *,
    id: str,
    thread_id: str | None = None,
    internal_date: datetime | None = None,
    label_ids: list[str] | None = None,
) -> dict:
    if internal_date is None:
        internal_date = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    raw_bytes = path.read_bytes()
    return {
        "id": id,
        "thread_id": thread_id or f"thread-{id}",
        "history_id": "0",  # overwritten by the port to whatever history id is current
        "internal_date": int(internal_date.timestamp() * 1000),
        "label_ids": label_ids or ["INBOX"],
        "snippet": "",
        "size_estimate": len(raw_bytes),
        "raw": raw_bytes,
    }


class FakeGmailPort:
    def __init__(self):
        self._messages: dict[str, dict] = {}
        self._history_id = 1
        self._events: list[tuple[int, str, str]] = []  # (history_id, "add"|"delete", id)
        self._min_retained_history_id = 1
        self._recent_ids: list[str] = []

    # --- test setup, not part of GmailPort ---

    def seed(self, message: dict) -> None:
        """A message present from the start — counts toward the initial backfill."""
        message = dict(message)
        message["history_id"] = str(self._history_id)
        self._messages[message["id"]] = message

    def deliver(self, message: dict) -> None:
        """A message arriving after the mailbox already has some history."""
        self._history_id += 1
        message = dict(message)
        message["history_id"] = str(self._history_id)
        self._messages[message["id"]] = message
        self._events.append((self._history_id, "add", message["id"]))
        self._recent_ids.append(message["id"])

    def delete(self, message_id: str) -> None:
        self._history_id += 1
        self._messages.pop(message_id, None)
        self._events.append((self._history_id, "delete", message_id))

    def expire_history_before(self, history_id: int) -> None:
        self._min_retained_history_id = history_id

    # --- GmailPort ---

    def get_profile(self) -> dict:
        return {"history_id": str(self._history_id)}

    def list_all_message_ids(self):
        return iter(list(self._messages.keys()))

    def list_recent_message_ids(self, days: int = gmail.HISTORY_FALLBACK_DAYS):
        return iter(list(self._recent_ids))

    def get_messages(self, ids: list[str]) -> dict[str, dict]:
        return {mid: self._messages[mid] for mid in ids if mid in self._messages}

    def collect_history(self, start_history_id: str) -> gmail.HistoryResult:
        start = int(start_history_id)
        if start < self._min_retained_history_id:
            raise gmail.HistoryExpired(f"history {start} no longer available")

        added, deleted = set(), set()
        for hid, kind, message_id in self._events:
            if hid <= start:
                continue
            (added if kind == "add" else deleted).add(message_id)

        return gmail.HistoryResult(
            added=added, deleted=deleted, new_history_id=str(self._history_id)
        )
