"""SQLite storage for the mail archive.

One file, `$LIFE_AGENT_STATE/mail.db` — WAL mode, FTS5 for full-text search, sqlite-vec
for embeddings (stage 5 populates `vec_chunks`; the table exists from the first
migration so schema and data don't drift out of sync across stages).

Messages are never deleted. A message Gmail no longer serves gets `deleted_at` set;
the row stays, because search over "mail I used to have" is a legitimate query and
because an accidental Gmail-side deletion should not silently destroy archive history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

EMBEDDING_DIMENSIONS = 1024

_SCHEMA_V1 = f"""
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT,
    history_id TEXT,
    internal_date INTEGER,
    date_iso TEXT,
    from_addr TEXT,
    from_name TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    reply_to TEXT,
    message_id_hdr TEXT,
    in_reply_to TEXT,
    references_hdr TEXT,
    subject TEXT,
    snippet TEXT,
    body_text TEXT,
    labels_json TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    attachments_json TEXT,
    size INTEGER,
    is_from_owner INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX messages_thread_id ON messages(thread_id);
CREATE INDEX messages_internal_date ON messages(internal_date);
CREATE INDEX messages_message_id_hdr ON messages(message_id_hdr);
CREATE INDEX messages_in_reply_to ON messages(in_reply_to);

-- External-content FTS5: messages.rowid (the implicit integer rowid, distinct from
-- the TEXT Gmail id in `id`) is the join key. Subject is its own column so search.py
-- can weight it above body text with bm25().
CREATE VIRTUAL TABLE messages_fts USING fts5(
    subject, from_addr, body_text,
    content='messages', content_rowid='rowid'
);

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, from_addr, body_text)
    VALUES (new.rowid, new.subject, new.from_addr, new.body_text);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, from_addr, body_text)
    VALUES ('delete', old.rowid, old.subject, old.from_addr, old.body_text);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, from_addr, body_text)
    VALUES ('delete', old.rowid, old.subject, old.from_addr, old.body_text);
    INSERT INTO messages_fts(rowid, subject, from_addr, body_text)
    VALUES (new.rowid, new.subject, new.from_addr, new.body_text);
END;

CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id),
    idx INTEGER NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX chunks_message_id ON chunks(message_id);

-- rowid of vec_chunks is kept equal to chunks.id by whoever inserts (embed.py, stage 5).
CREATE VIRTUAL TABLE vec_chunks USING vec0(
    embedding float[{EMBEDDING_DIMENSIONS}]
);

CREATE TABLE tags (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    category TEXT,
    importance INTEGER,
    summary TEXT,
    action TEXT,
    deadline TEXT,
    people_json TEXT,
    model TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    tagged_at TEXT,
    error TEXT
);

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    message_id TEXT,
    verdict TEXT,
    note TEXT,
    source_msg_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('vip', 'mute', 'topic')),
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT
);

CREATE TABLE digests (
    date TEXT PRIMARY KEY,
    message_id_hdr TEXT,
    refs_json TEXT,
    path TEXT,
    inserted_gmail_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE pending (
    id TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
-- Lets a digest note "you asked me to draft a reply to #n" exactly once, in the next
-- digest, without re-deriving which item #n was (feedback.py doesn't keep that
-- mapping past the reply that referenced it).
ALTER TABLE feedback ADD COLUMN acknowledged_at TEXT;
"""

# (version, schema script) in order. Applied once each, tracked in schema_migrations.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
]

_MESSAGE_COLUMNS = (
    "id", "thread_id", "history_id", "internal_date", "date_iso", "from_addr",
    "from_name", "to_addrs", "cc_addrs", "reply_to", "message_id_hdr", "in_reply_to",
    "references_hdr", "subject", "snippet", "body_text", "labels_json",
    "has_attachments", "attachments_json", "size", "is_from_owner", "fetched_at",
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, script in MIGRATIONS:
        if version in applied:
            continue
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )
        conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the mail database with the full schema applied."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    _apply_migrations(conn)
    return conn


def upsert_message(conn: sqlite3.Connection, message: dict) -> None:
    """Insert a message, or update it in place if its id already exists.

    Never touches `deleted_at` — that is exclusively `mark_deleted`'s / `mark_undeleted`'s
    job, so re-fetching a message during sync can't silently undo a deletion (or, more
    likely in practice, silently paper over one that should have been investigated).
    """
    missing = [c for c in _MESSAGE_COLUMNS if c not in message]
    if missing:
        raise ValueError(f"upsert_message: missing required field(s): {missing}")

    columns = _MESSAGE_COLUMNS
    placeholders = ", ".join(f":{c}" for c in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "id")

    conn.execute(
        f"INSERT INTO messages ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_clause}",
        message,
    )
    conn.commit()


def mark_deleted(conn: sqlite3.Connection, message_id: str, deleted_at: str) -> None:
    conn.execute(
        "UPDATE messages SET deleted_at = ? WHERE id = ?", (deleted_at, message_id)
    )
    conn.commit()


def get_message(conn: sqlite3.Connection, message_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def count_messages(conn: sqlite3.Connection, include_deleted: bool = True) -> int:
    if include_deleted:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"
    ).fetchone()[0]


_TAG_COLUMNS = (
    "message_id", "category", "importance", "summary", "action", "deadline",
    "people_json", "model", "attempts", "tagged_at", "error",
)


def get_tag(conn: sqlite3.Connection, message_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tags WHERE message_id = ?", (message_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_tag(conn: sqlite3.Connection, tag: dict) -> None:
    missing = [c for c in _TAG_COLUMNS if c not in tag]
    if missing:
        raise ValueError(f"upsert_tag: missing required field(s): {missing}")

    columns = _TAG_COLUMNS
    placeholders = ", ".join(f":{c}" for c in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "message_id")

    conn.execute(
        f"INSERT INTO tags ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(message_id) DO UPDATE SET {update_clause}",
        tag,
    )
    conn.commit()


def get_recent_feedback(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def add_feedback(
    conn: sqlite3.Connection, *, message_id: str, verdict: str, note: str | None,
    source_msg_id: str, created_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO feedback(message_id, verdict, note, source_msg_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (message_id, verdict, note, source_msg_id, created_at),
    )
    conn.commit()
    return cursor.lastrowid


def get_unacknowledged_reply_feedback(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM feedback WHERE verdict = 'reply' AND acknowledged_at IS NULL"
    ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_feedback(conn: sqlite3.Connection, feedback_ids: list[int], when: str) -> None:
    if not feedback_ids:
        return
    conn.executemany(
        "UPDATE feedback SET acknowledged_at = ? WHERE id = ?",
        [(when, fid) for fid in feedback_ids],
    )
    conn.commit()


def add_rule(
    conn: sqlite3.Connection, *, kind: str, value: str, created_at: str, source: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO rules(kind, value, created_at, source) VALUES (?, ?, ?, ?)",
        (kind, value, created_at, source),
    )
    conn.commit()
    return cursor.lastrowid


def get_rule_values(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT value FROM rules WHERE kind = ?", (kind,)).fetchall()
    return {row["value"].lower() for row in rows}


_DIGEST_COLUMNS = ("date", "message_id_hdr", "refs_json", "path", "inserted_gmail_id", "created_at")


def upsert_digest(conn: sqlite3.Connection, digest: dict) -> None:
    missing = [c for c in _DIGEST_COLUMNS if c not in digest]
    if missing:
        raise ValueError(f"upsert_digest: missing required field(s): {missing}")

    columns = _DIGEST_COLUMNS
    placeholders = ", ".join(f":{c}" for c in columns)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "date")

    conn.execute(
        f"INSERT INTO digests ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(date) DO UPDATE SET {update_clause}",
        digest,
    )
    conn.commit()


def get_digest(conn: sqlite3.Connection, date_str: str) -> dict | None:
    row = conn.execute("SELECT * FROM digests WHERE date = ?", (date_str,)).fetchone()
    return dict(row) if row else None


def get_latest_digest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM digests ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
