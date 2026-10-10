import pytest

from src.mail import embed, store
from tests.mail.fake_embedder import FakeEmbedder


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "mail.db")
    yield c
    c.close()


@pytest.fixture
def embedder():
    return FakeEmbedder()


def test_build_chunk_texts_includes_subject_and_from(message_factory, embedder):
    message = message_factory("m1", subject="Roof gutter quote", from_name="Hartmann",
                               from_addr="info@hartmann.example", body_text="short body")
    chunks = embed.build_chunk_texts(message, embedder)
    assert len(chunks) == 1
    assert "Roof gutter quote" in chunks[0]
    assert "Hartmann" in chunks[0]
    assert "short body" in chunks[0]


def test_build_chunk_texts_handles_empty_body(message_factory, embedder):
    message = message_factory("m1", subject="ok", body_text="")
    chunks = embed.build_chunk_texts(message, embedder)
    assert len(chunks) == 1  # header-only chunk, not zero


def test_build_chunk_texts_caps_at_max_chunks(message_factory, embedder):
    long_body = " ".join(f"word{i}" for i in range(500))
    message = message_factory("m1", subject="long", body_text=long_body)
    chunks = embed.build_chunk_texts(message, embedder)
    assert len(chunks) <= embed.MAX_CHUNKS_PER_MESSAGE


def test_embed_pending_populates_chunks_and_vec_table(conn, embedder, message_factory):
    store.upsert_message(conn, message_factory("m1", subject="Roof gutter quote",
                                                body_text="quote from Hartmann"))
    store.upsert_message(conn, message_factory("m2", subject="Lunch", body_text="Thursday noon"))

    processed = embed.embed_pending(conn, embedder)
    assert processed == 2

    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert chunk_count == 2  # one short chunk each

    vec_count = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    assert vec_count == chunk_count


def test_embed_pending_skips_already_embedded_messages(conn, embedder, message_factory):
    store.upsert_message(conn, message_factory("m1", subject="a", body_text="a"))
    embed.embed_pending(conn, embedder)
    assert embed.embed_pending(conn, embedder) == 0  # nothing left to do


def test_embed_pending_respects_budget(conn, embedder, message_factory):
    for i in range(5):
        store.upsert_message(conn, message_factory(f"m{i}", subject=f"s{i}", body_text=f"b{i}"))

    processed = embed.embed_pending(conn, embedder, budget_seconds=0)
    assert processed == 0
    remaining = conn.execute(
        "SELECT COUNT(*) FROM messages m LEFT JOIN chunks c ON c.message_id = m.id "
        "WHERE c.id IS NULL"
    ).fetchone()[0]
    assert remaining == 5

    processed = embed.embed_pending(conn, embedder)
    assert processed == 5


def test_recent_messages_embedded_before_older_ones(conn, embedder, message_factory):
    import time

    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    old = message_factory("old", subject="old", body_text="old",
                           internal_date=now_ms - 60 * day_ms)
    recent = message_factory("recent", subject="recent", body_text="recent",
                              internal_date=now_ms - 1 * day_ms)
    store.upsert_message(conn, old)
    store.upsert_message(conn, recent)

    order = embed._messages_needing_embeddings(conn, now_ms - 30 * day_ms)
    assert order == ["recent", "old"]


# --- batching (docs/plans/mail-personal-v1.md stage 2) ---


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(len(texts))
        return super().embed(texts)


def _seed(conn, message_factory, count, words=10):
    import time

    now_ms = int(time.time() * 1000)
    for i in range(count):
        body = " ".join(f"w{i}x{j}" for j in range(words))
        store.upsert_message(conn, message_factory(
            f"m{i:03d}", subject=f"subject {i}", body_text=body,
            internal_date=now_ms - i * 60_000,
        ))


def _dump(conn):
    rows = conn.execute(
        "SELECT c.message_id, c.idx, c.text, v.embedding FROM chunks c "
        "JOIN vec_chunks v ON v.rowid = c.id ORDER BY c.message_id, c.idx"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_batched_path_writes_the_same_chunks_and_vectors_as_per_message(
        tmp_path, message_factory):
    per_message = store.connect(tmp_path / "a.db")
    batched = store.connect(tmp_path / "b.db")
    for conn in (per_message, batched):
        _seed(conn, message_factory, 20, words=100)  # 3 chunks each with the fake

    assert embed.embed_pending(per_message, FakeEmbedder(), batch_chunks=1) == 20
    counting = CountingEmbedder()
    assert embed.embed_pending(batched, counting, batch_chunks=32) == 20

    assert _dump(batched) == _dump(per_message)
    assert len(_dump(batched)) == 60
    assert len(counting.calls) < 20  # chunks from several messages per model call
    assert max(counting.calls) <= 32 + embed.MAX_CHUNKS_PER_MESSAGE - 1
    per_message.close()
    batched.close()


def test_budget_cut_off_mid_batch_commits_the_batch_and_resumes(conn, message_factory,
                                                                 monkeypatch):
    _seed(conn, message_factory, 10)  # 1 chunk each -> batches of 4 with batch_chunks=4
    clock = [0.0]
    monkeypatch.setattr(embed.time, "monotonic", lambda: clock[0])

    class ClockEmbedder(FakeEmbedder):
        def embed(self, texts):
            clock[0] += 100.0  # the budget runs out while this batch is being encoded
            return super().embed(texts)

    processed = embed.embed_pending(conn, ClockEmbedder(), budget_seconds=50, batch_chunks=4)
    assert processed == 4  # the batch in flight is finished and committed, no more
    assert conn.execute("SELECT COUNT(DISTINCT message_id) FROM chunks").fetchone()[0] == 4

    processed = embed.embed_pending(conn, FakeEmbedder(), batch_chunks=4)
    assert processed == 6
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 10


def test_message_embedded_meanwhile_is_not_duplicated(conn, message_factory, monkeypatch):
    _seed(conn, message_factory, 3)
    real_write = embed._write_message_chunks

    def write_twice(c, message_id, texts, vectors):
        real_write(c, message_id, texts, vectors)  # "another run" got there first
        return real_write(c, message_id, texts, vectors)

    monkeypatch.setattr(embed, "_write_message_chunks", write_twice)
    assert embed.embed_pending(conn, FakeEmbedder()) == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3


def test_measure_reports_rate_and_commits_nothing(tmp_path, message_factory):
    db_path = tmp_path / "mail.db"
    conn = store.connect(db_path)
    _seed(conn, message_factory, 12, words=100)
    conn.close()

    conn = store.connect(db_path)
    result = embed.measure(conn, FakeEmbedder(), 5)
    conn.rollback()
    conn.close()

    assert result["messages"] == 5
    assert result["chunks"] == 15
    assert result["messages_per_minute"] > 0
    assert result["backlog"] == 12

    check = store.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert check.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    check.close()


def test_rss_helpers_report_something_on_linux():
    assert embed.current_rss_bytes() > 0
    assert embed.peak_rss_bytes() >= embed.current_rss_bytes() // 2
