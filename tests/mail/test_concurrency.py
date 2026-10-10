"""Sync and embed are separate timers (docs/plans/mail-personal-v1.md stage 2) and
can run at the same moment against the same mail.db. WAL plus the busy timeout in
store.connect must let both finish with nothing lost and nothing corrupted.
"""

import threading
import time
from pathlib import Path

from src.mail import embed, gmail, rawstore, store
from src.mail.config import load_config
from tests.mail.fake_embedder import FakeEmbedder
from tests.mail.fake_gmail import FakeGmailPort, raw_message

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


class SlowPort(FakeGmailPort):
    def get_messages(self, ids):
        time.sleep(0.01)
        return super().get_messages(ids)


class SlowEmbedder(FakeEmbedder):
    def embed(self, texts):
        time.sleep(0.005)
        return super().embed(texts)


def test_connect_sets_a_busy_timeout(tmp_path):
    conn = store.connect(tmp_path / "mail.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_writer_waits_for_a_held_write_lock_instead_of_failing(tmp_path, message_factory):
    db_path = tmp_path / "mail.db"
    store.connect(db_path).close()
    locked = threading.Event()

    def hold_write_lock():
        holder = store.connect(db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO sync_state(key, value) VALUES ('x', '1')")
        locked.set()
        time.sleep(0.5)
        holder.commit()
        holder.close()

    thread = threading.Thread(target=hold_write_lock)
    thread.start()
    locked.wait(timeout=5)

    waiter = store.connect(db_path)
    started = time.monotonic()
    store.upsert_message(waiter, message_factory("m1"))  # blocks until the commit
    assert time.monotonic() - started >= 0.3
    thread.join()

    assert store.count_messages(waiter) == 1
    waiter.close()


def test_sync_and_embed_running_at_the_same_time(env_dirs, tmp_path):
    config = load_config(env_dirs["data"])
    db_path = tmp_path / "mail.db"
    fixture_files = sorted(FIXTURES.glob("*.eml"))

    port = SlowPort()
    total = 300
    for i in range(total):
        port.seed(raw_message(fixture_files[i % len(fixture_files)], id=f"m{i:04d}",
                              internal_date=None))

    # Some messages already stored, so the embedder has work from the first moment.
    setup_conn = store.connect(db_path)
    gmail.sync(setup_conn, port, config, budget_seconds=0)  # discovery only
    setup_conn.close()

    errors = []
    embedded = []
    sync_done = threading.Event()

    def run_sync():
        conn = store.connect(db_path)
        try:
            gmail.sync(conn, port, config)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(("sync", exc))
        finally:
            conn.close()
            sync_done.set()

    def run_embed():
        conn = store.connect(db_path)
        try:
            # Keep embedding while sync is still adding messages, then once more.
            while not sync_done.is_set():
                embedded.append(embed.embed_pending(conn, SlowEmbedder(), budget_seconds=0.2))
            embedded.append(embed.embed_pending(conn, SlowEmbedder()))
        except Exception as exc:  # noqa: BLE001
            errors.append(("embed", exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=run_sync), threading.Thread(target=run_embed)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    assert sum(embedded) == total
    assert sum(1 for n in embedded[:-1] if n) > 0  # some embedding overlapped the sync

    conn = store.connect(db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.count_messages(conn) == total
    unembedded = conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.message_id = m.id)"
    ).fetchone()[0]
    assert unembedded == 0
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == chunk_count
    conn.close()
    assert len(rawstore.list_raw_ids(tmp_path / "raw")) == total
