import sqlite3

import sqlite_vec
import pytest

from src.mail import store


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "mail.db")
    yield c
    c.close()


def test_connect_creates_state_dir_and_db(tmp_path):
    db_path = tmp_path / "nested" / "mail.db"
    c = store.connect(db_path)
    c.close()
    assert db_path.exists()


def test_connect_is_idempotent(tmp_path):
    db_path = tmp_path / "mail.db"
    store.connect(db_path).close()
    # Reopening an existing db must not re-run migrations or error.
    conn2 = store.connect(db_path)
    applied = conn2.execute("SELECT version FROM schema_migrations").fetchall()
    conn2.close()
    assert [row[0] for row in applied] == [v for v, _ in store.MIGRATIONS]


def test_upsert_message_requires_all_columns(conn):
    with pytest.raises(ValueError, match="missing required field"):
        store.upsert_message(conn, {"id": "m1"})


def test_upsert_then_get(conn, message_factory):
    store.upsert_message(conn, message_factory("m1", subject="Hello"))
    row = store.get_message(conn, "m1")
    assert row is not None
    assert row["subject"] == "Hello"
    assert row["deleted_at"] is None


def test_upsert_updates_existing_row(conn, message_factory):
    store.upsert_message(conn, message_factory("m1", subject="Original"))
    store.upsert_message(conn, message_factory("m1", subject="Updated"))
    assert store.count_messages(conn) == 1
    assert store.get_message(conn, "m1")["subject"] == "Updated"


def test_mark_deleted_preserves_row(conn, message_factory):
    store.upsert_message(conn, message_factory("m1"))
    store.mark_deleted(conn, "m1", "2026-08-20T00:00:00Z")

    row = store.get_message(conn, "m1")
    assert row["deleted_at"] == "2026-08-20T00:00:00Z"
    assert store.count_messages(conn, include_deleted=True) == 1
    assert store.count_messages(conn, include_deleted=False) == 0


def test_reupsert_does_not_undelete(conn, message_factory):
    store.upsert_message(conn, message_factory("m1"))
    store.mark_deleted(conn, "m1", "2026-08-20T00:00:00Z")
    store.upsert_message(conn, message_factory("m1", subject="Refetched"))

    row = store.get_message(conn, "m1")
    assert row["subject"] == "Refetched"
    assert row["deleted_at"] == "2026-08-20T00:00:00Z"


def test_fts_finds_inserted_message(conn, message_factory):
    store.upsert_message(
        conn, message_factory("m1", subject="Roof gutter quote", body_text="Hartmann Dachbau")
    )
    store.upsert_message(
        conn, message_factory("m2", subject="Lunch on Thursday", body_text="new place")
    )

    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'gutter'"
    ).fetchall()
    assert len(hits) == 1

    message_id = conn.execute(
        "SELECT id FROM messages WHERE rowid = ?", (hits[0]["rowid"],)
    ).fetchone()["id"]
    assert message_id == "m1"


def test_fts_updates_when_message_updates(conn, message_factory):
    store.upsert_message(conn, message_factory("m1", subject="Original subject"))
    store.upsert_message(conn, message_factory("m1", subject="Completely different"))

    assert not conn.execute(
        "SELECT 1 FROM messages_fts WHERE messages_fts MATCH 'Original'"
    ).fetchall()
    assert conn.execute(
        "SELECT 1 FROM messages_fts WHERE messages_fts MATCH 'different'"
    ).fetchall()


def test_vec_chunks_table_accepts_embeddings(conn, message_factory):
    store.upsert_message(conn, message_factory("m1"))
    chunk_id = conn.execute(
        "INSERT INTO chunks(message_id, idx, text) VALUES ('m1', 0, 'chunk text')"
    ).lastrowid
    conn.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32([0.1] * store.EMBEDDING_DIMENSIONS)),
    )
    conn.commit()

    row = conn.execute("SELECT rowid FROM vec_chunks WHERE rowid = ?", (chunk_id,)).fetchone()
    assert row is not None


def test_wal_db_opens_mode_ro_after_writer_closes(tmp_path, message_factory):
    """serve.py's query socket opens mail.db `mode=ro` — this is the thing that
    actually enforces read-only access; the systemd unit must not *also* bind-mount
    the state dir read-only (ReadOnlyPaths=), because SQLite may still need to create
    a -shm file for WAL locking even on a logically read-only connection, and a
    read-only bind mount blocks that at the OS level regardless of what SQLite asks
    for. This test only checks the SQLite/WAL side of that; the systemd sandboxing
    side isn't something a unit test can exercise.
    """
    db_path = tmp_path / "mail.db"
    writer = store.connect(db_path)
    store.upsert_message(writer, message_factory("m1"))
    writer.close()  # simulates a checkpoint/close between sync and a query

    reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    reader.enable_load_extension(True)
    sqlite_vec.load(reader)
    reader.enable_load_extension(False)

    row = reader.execute("SELECT id FROM messages WHERE id = ?", ("m1",)).fetchone()
    assert row["id"] == "m1"

    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO messages(id, fetched_at) VALUES ('m2', 'x')")

    reader.close()
