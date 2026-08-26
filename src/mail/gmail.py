"""Gmail OAuth + sync.

Sync logic (`sync()` and everything it calls) is written against `GmailPort` — a small
duck-typed interface with exactly the operations sync needs — rather than directly
against `googleapiclient`'s dynamically-built `Resource` objects. `RealGmailPort` wraps
the real API; tests use a fake port that serves `examples/mail/*.eml`. This is the
"fake Gmail client" the plan asks for: it fakes the port, not Google's HTTP/discovery
machinery, which would be far more code to get right for no testing benefit.

Deviation from the plan text worth flagging: messages are fetched with
`format="raw"` rather than `format="full"`. `extract.py` (stage 4) already parses raw
RFC 822 bytes via `email.policy` — reconstructing an equivalent byte stream from
Gmail's `format=full` JSON payload tree would mean re-implementing MIME assembly for no
benefit. `snippet`/`labelIds`/`historyId`/`internalDate` are present in the message
resource regardless of format, so nothing is lost.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from src.mail import extract, store
from src.mail.config import MailConfig

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
INSERT_SCOPE = "https://www.googleapis.com/auth/gmail.insert"

READONLY_TOKEN_FILENAME = "gmail-readonly-token.json"
INSERT_TOKEN_FILENAME = "gmail-insert-token.json"
CLIENT_SECRETS_FILENAME = "google-client.json"

BATCH_SIZE = 50
LIST_PAGE_SIZE = 500
HISTORY_FALLBACK_DAYS = 2
NUM_RETRIES = 5  # googleapiclient's built-in exponential backoff on 429/5xx


class GmailAuthError(Exception):
    """Something needed for auth is missing — always something only the owner can fix."""


class HistoryExpired(Exception):
    """Gmail's history.list returned 404 — the startHistoryId is too old to resume from."""


@dataclass(frozen=True)
class HistoryResult:
    added: set[str]
    deleted: set[str]
    new_history_id: str


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    mode: str  # "backfill" | "incremental" | "incremental-fallback"
    done: bool


# ---------------------------------------------------------------------------
# GmailPort: the interface sync() is written against.
#
#   get_profile() -> {"history_id": str}
#   list_all_message_ids() -> Iterator[str]            (pages internally)
#   list_recent_message_ids(days: int) -> Iterator[str] (pages internally)
#   get_messages(ids: list[str]) -> dict[str, dict]     (batched; missing ids simply
#                                                         absent from the result, e.g.
#                                                         a message deleted mid-fetch)
#   collect_history(start_history_id: str) -> HistoryResult, raises HistoryExpired
#
# Each raw message dict: id, thread_id, history_id, internal_date (int, epoch ms),
# label_ids (list[str]), snippet (str), size_estimate (int), raw (bytes, RFC 822).
# ---------------------------------------------------------------------------


class RealGmailPort:
    """GmailPort backed by the real Gmail API. Not exercised by tests — there is no
    live mailbox to test against in this environment. Verify with a real
    `mail sync --budget 60` once the readonly token exists.
    """

    def __init__(self, service, user_id: str = "me"):
        self._service = service
        self._user_id = user_id

    def get_profile(self) -> dict:
        profile = self._service.users().getProfile(userId=self._user_id).execute(
            num_retries=NUM_RETRIES
        )
        return {"history_id": profile["historyId"]}

    def _list_ids(self, query: str | None) -> Iterator[str]:
        page_token = None
        while True:
            request = self._service.users().messages().list(
                userId=self._user_id,
                includeSpamTrash=False,
                maxResults=LIST_PAGE_SIZE,
                pageToken=page_token,
                q=query,
            )
            response = request.execute(num_retries=NUM_RETRIES)
            for message in response.get("messages", []):
                yield message["id"]
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def list_all_message_ids(self) -> Iterator[str]:
        return self._list_ids(query=None)

    def list_recent_message_ids(self, days: int = HISTORY_FALLBACK_DAYS) -> Iterator[str]:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
        return self._list_ids(query=f"after:{cutoff}")

    def get_messages(self, ids: list[str]) -> dict[str, dict]:
        import base64

        results: dict[str, dict] = {}
        errors: dict[str, Exception] = {}

        def _callback(request_id, response, exception):
            if exception is not None:
                errors[request_id] = exception
                return
            results[request_id] = {
                "id": response["id"],
                "thread_id": response["threadId"],
                "history_id": response["historyId"],
                "internal_date": int(response["internalDate"]),
                "label_ids": response.get("labelIds", []),
                "snippet": response.get("snippet", ""),
                "size_estimate": response.get("sizeEstimate"),
                "raw": base64.urlsafe_b64decode(response["raw"] + "=="),
            }

        batch = self._service.new_batch_http_request(callback=_callback)
        for message_id in ids:
            batch.add(
                self._service.users().messages().get(
                    userId=self._user_id, id=message_id, format="raw"
                ),
                request_id=message_id,
            )
        batch.execute(num_retries=NUM_RETRIES)

        if errors:
            # A message that 404s (deleted between listing and fetch) is not an error
            # worth failing the whole batch over; anything else propagates.
            from googleapiclient.errors import HttpError

            hard_errors = {
                mid: exc
                for mid, exc in errors.items()
                if not (isinstance(exc, HttpError) and exc.resp.status == 404)
            }
            if hard_errors:
                raise next(iter(hard_errors.values()))

        return results

    def collect_history(self, start_history_id: str) -> HistoryResult:
        from googleapiclient.errors import HttpError

        added: set[str] = set()
        deleted: set[str] = set()
        new_history_id = start_history_id
        page_token = None

        while True:
            try:
                response = self._service.users().history().list(
                    userId=self._user_id,
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded", "messageDeleted"],
                    pageToken=page_token,
                ).execute(num_retries=NUM_RETRIES)
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpired(str(exc)) from exc
                raise

            for record in response.get("history", []):
                for added_record in record.get("messagesAdded", []):
                    added.add(added_record["message"]["id"])
                for deleted_record in record.get("messagesDeleted", []):
                    deleted.add(deleted_record["message"]["id"])

            new_history_id = response.get("historyId", new_history_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return HistoryResult(added=added, deleted=deleted, new_history_id=new_history_id)


def build_service(credentials):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def insert_raw_message(
    service, raw_bytes: bytes, label_ids: tuple[str, ...] = ("INBOX", "UNREAD")
) -> str:
    """Inserts a message the archive composed itself (the digest) into the owner's own
    inbox — never a message read from elsewhere. Requires the gmail.insert-scoped
    service/token, distinct from the readonly one everything else uses.
    """
    import base64

    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    response = service.users().messages().insert(
        userId="me", body={"raw": encoded, "labelIds": list(label_ids)}
    ).execute(num_retries=NUM_RETRIES)
    return response["id"]


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def run_auth_flow(conf_dir: Path, scope: str, token_filename: str) -> Path:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_path = conf_dir / CLIENT_SECRETS_FILENAME
    if not secrets_path.is_file():
        raise GmailAuthError(
            f"missing {secrets_path} — create a Google Cloud OAuth client (Desktop app) "
            f"with the Gmail API enabled and the {scope} scope, download the JSON here, "
            "mode 0600"
        )

    print(
        "Reminder: the OAuth consent screen must be in Production publishing status — "
        "otherwise this refresh token expires after 7 days.\n"
        "This opens a local server on port 8765. From your PC:\n"
        "  ssh -L 8765:localhost:8765 <this-host>\n"
        "then open the printed URL in a browser on your PC.\n"
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes=[scope])
    credentials = flow.run_local_server(port=8765, open_browser=False)

    token_path = conf_dir / token_filename
    token_path.write_text(credentials.to_json())
    token_path.chmod(0o600)
    print(f"wrote {token_path}")
    return token_path


def auth_readonly(conf_dir: Path) -> Path:
    return run_auth_flow(conf_dir, READONLY_SCOPE, READONLY_TOKEN_FILENAME)


def auth_insert(conf_dir: Path) -> Path:
    return run_auth_flow(conf_dir, INSERT_SCOPE, INSERT_TOKEN_FILENAME)


def load_credentials(conf_dir: Path, token_filename: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = conf_dir / token_filename
    if not token_path.is_file():
        raise GmailAuthError(f"no token at {token_path} — run `mail auth ...` first")

    credentials = Credentials.from_authorized_user_file(str(token_path))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json())
        token_path.chmod(0o600)
    return credentials


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _internal_date_to_iso(internal_date_ms: int) -> str:
    return datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc).isoformat()


def _make_deadline(budget_seconds: float | None) -> float | None:
    return None if budget_seconds is None else time.monotonic() + budget_seconds


def _deadline_passed(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def get_sync_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_sync_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _queue_ids(conn, ids) -> None:
    now = _now_iso()
    for message_id in ids:
        conn.execute(
            "INSERT OR IGNORE INTO pending(id, added_at) VALUES (?, ?)", (message_id, now)
        )
    conn.commit()


def _store_message(conn, config: MailConfig, raw_message: dict) -> None:
    fields = extract.build_message_fields(raw_message["raw"])
    is_from_owner = fields["from_addr"].strip().lower() == config.address.strip().lower()

    message = {
        **fields,
        "id": raw_message["id"],
        "thread_id": raw_message["thread_id"],
        "history_id": str(raw_message["history_id"]),
        "internal_date": raw_message["internal_date"],
        "date_iso": _internal_date_to_iso(raw_message["internal_date"]),
        "snippet": raw_message.get("snippet", ""),
        "labels_json": json.dumps(raw_message.get("label_ids") or []),
        "size": raw_message.get("size_estimate"),
        "is_from_owner": 1 if is_from_owner else 0,
        "fetched_at": _now_iso(),
    }
    store.upsert_message(conn, message)


def _drain_pending(conn, port, config: MailConfig, deadline: float | None) -> int:
    fetched = 0
    while not _deadline_passed(deadline):
        rows = conn.execute(
            "SELECT id FROM pending ORDER BY added_at LIMIT ?", (BATCH_SIZE,)
        ).fetchall()
        if not rows:
            break

        ids = [row["id"] for row in rows]
        raw_messages = port.get_messages(ids)
        for message_id in ids:
            raw = raw_messages.get(message_id)
            if raw is not None:
                _store_message(conn, config, raw)
            # else: gone between listing and fetch (e.g. deleted) — drop from pending.

        conn.executemany("DELETE FROM pending WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        fetched += len(ids)

    return fetched


def sync(conn, port, config: MailConfig, budget_seconds: float | None = None) -> SyncResult:
    deadline = _make_deadline(budget_seconds)
    fetched = 0

    if get_sync_state(conn, "backfill_complete") != "1":
        if get_sync_state(conn, "history_id") is None:
            set_sync_state(conn, "history_id", str(port.get_profile()["history_id"]))
            _queue_ids(conn, port.list_all_message_ids())

        fetched += _drain_pending(conn, port, config, deadline)
        pending_left = conn.execute("SELECT 1 FROM pending LIMIT 1").fetchone() is not None
        if not pending_left:
            set_sync_state(conn, "backfill_complete", "1")
        return SyncResult(fetched=fetched, mode="backfill", done=not pending_left)

    # Anything left in `pending` from a previous run that hit its budget.
    fetched += _drain_pending(conn, port, config, deadline)
    if _deadline_passed(deadline):
        return SyncResult(fetched=fetched, mode="incremental", done=False)

    history_id = get_sync_state(conn, "history_id")
    fallback_used = False
    new_history_id = None
    try:
        history = port.collect_history(history_id)
        added, deleted = history.added, history.deleted
        new_history_id = history.new_history_id
    except HistoryExpired:
        fallback_used = True
        added, deleted = set(port.list_recent_message_ids(HISTORY_FALLBACK_DAYS)), set()

    for message_id in deleted:
        store.mark_deleted(conn, message_id, _now_iso())

    _queue_ids(conn, added)
    fetched += _drain_pending(conn, port, config, deadline)

    set_sync_state(
        conn, "history_id", str(new_history_id) if new_history_id is not None
        else str(port.get_profile()["history_id"])
    )

    return SyncResult(
        fetched=fetched,
        mode="incremental-fallback" if fallback_used else "incremental",
        done=not _deadline_passed(deadline),
    )
