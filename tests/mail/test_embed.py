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

    order = [row["id"] for row in embed._messages_needing_embeddings(conn, now_ms - 30 * day_ms)]
    assert order == ["recent", "old"]
