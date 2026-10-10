"""Chunking + embedding. `SentenceTransformerEmbedder` (BAAI/bge-m3, GPU if available)
is the real thing; tests use a fake embedder that needs no model download — see the
plan's stage 5 verification note. Both satisfy the same tiny interface:
`chunk(text) -> list[str]` and `embed(texts) -> list[vector]`, vectors matching
`store.EMBEDDING_DIMENSIONS`.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import sqlite_vec

from src.mail import store

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-m3"
MAX_CHUNKS_PER_MESSAGE = 8
CHUNK_TOKENS = 500
RECENT_DAYS_FIRST = 30
# Chunks per model call. Batching across messages is what makes CPU embedding usable;
# 32 keeps a batch's activations well inside the embed unit's MemoryMax.
BATCH_CHUNKS = 32


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # respects CPU pinning / cgroup cpusets
    except AttributeError:
        return os.cpu_count() or 1


class SentenceTransformerEmbedder:
    """BAAI/bge-m3 via sentence-transformers. fp16 on CUDA; CPU with a loud warning
    otherwise (multilingual embedding on CPU is usable for a personal mailbox's scale,
    just slow — v1 doesn't require the GPU, it just expects one on this host).
    """

    def __init__(self, hf_home: Path):
        hf_home.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(hf_home))

        import torch
        from sentence_transformers import SentenceTransformer

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            torch.set_num_threads(_cpu_count())
            logger.warning(
                "CUDA not available — running %s on CPU (%d threads); this will be slow",
                MODEL_NAME, torch.get_num_threads(),
            )

        self.model = SentenceTransformer(MODEL_NAME, device=self.device)
        if self.device == "cuda":
            self.model = self.model.half()

    def chunk(self, text: str, max_chunks: int = MAX_CHUNKS_PER_MESSAGE) -> list[str]:
        if not text.strip():
            return []
        tokenizer = self.model.tokenizer
        token_ids = tokenizer.encode(text, add_special_tokens=False)
        chunks = []
        for i in range(0, len(token_ids), CHUNK_TOKENS):
            if len(chunks) >= max_chunks:
                break
            chunks.append(tokenizer.decode(token_ids[i : i + CHUNK_TOKENS]))
        return chunks

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            texts, batch_size=BATCH_CHUNKS, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vectors]


def build_chunk_texts(message: dict, embedder) -> list[str]:
    """Subject + from as context prepended to every chunk (so a fragment of a long
    body still carries who it's from and what it's about), body chunked at ~500 tokens,
    capped at 8 chunks/mail.
    """
    prefix = f"Subject: {message['subject']}\nFrom: {message['from_name']} <{message['from_addr']}>\n\n"
    body_chunks = embedder.chunk(message.get("body_text") or "")
    if not body_chunks:
        return [prefix.strip()]
    return [prefix + chunk for chunk in body_chunks]


def _messages_needing_embeddings(conn, cutoff_ms: int) -> list[str]:
    """Ids, newest-first within the last `RECENT_DAYS_FIRST` days, then oldest-first
    for everything older: recent mail becomes searchable fast, and the long tail
    backfills in a stable order. Ids only; each row is loaded when its batch is built,
    so a large backlog never sits in memory as full rows.
    """
    recent = conn.execute(
        "SELECT m.id FROM messages m "
        "WHERE m.deleted_at IS NULL AND m.internal_date >= ? "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_id = m.id) "
        "ORDER BY m.internal_date DESC",
        (cutoff_ms,),
    ).fetchall()
    older = conn.execute(
        "SELECT m.id FROM messages m "
        "WHERE m.deleted_at IS NULL AND m.internal_date < ? "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_id = m.id) "
        "ORDER BY m.internal_date ASC",
        (cutoff_ms,),
    ).fetchall()
    return [row["id"] for row in recent] + [row["id"] for row in older]


def _iter_batches(conn, ids: list[str], embedder, batch_chunks: int):
    """Yields lists of (message_id, chunk_texts), each list holding at least
    `batch_chunks` chunks (except the last). A message's chunks are never split
    across batches, so a batch is up to `batch_chunks + MAX_CHUNKS_PER_MESSAGE - 1`.
    """
    batch: list[tuple[str, list[str]]] = []
    size = 0
    for message_id in ids:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            continue
        texts = build_chunk_texts(dict(row), embedder)
        batch.append((message_id, texts))
        size += len(texts)
        if size >= batch_chunks:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def _write_message_chunks(conn, message_id: str, texts: list[str], vectors) -> bool:
    """One message's chunks + vectors, one commit. Skips a message that gained chunks
    since it was listed (another embed run got there first). Returns True if written.
    """
    if conn.execute("SELECT 1 FROM chunks WHERE message_id = ? LIMIT 1",
                    (message_id,)).fetchone():
        return False
    for idx, (text, vector) in enumerate(zip(texts, vectors)):
        chunk_id = conn.execute(
            "INSERT INTO chunks(message_id, idx, text) VALUES (?, ?, ?)",
            (message_id, idx, text),
        ).lastrowid
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(vector)),
        )
    conn.commit()
    return True


def embed_pending(conn, embedder, budget_seconds: float | None = None,
                  batch_chunks: int = BATCH_CHUNKS) -> int:
    """Embed every message that has no chunks yet. Returns the number of messages
    processed.

    Chunks from several messages go to the model in one `embed` call (about
    `batch_chunks` at a time), which is what makes CPU embedding bearable. Writes stay
    per message, one commit each, so a run killed mid-way (budget, OOM, shutdown)
    loses at most the batch in flight, and a re-run skips every message already
    embedded. The budget is checked before each batch: a batch that started is
    finished and committed.
    """
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    cutoff_ms = int((time.time() - RECENT_DAYS_FIRST * 86400) * 1000)
    ids = _messages_needing_embeddings(conn, cutoff_ms)

    processed = 0
    for batch in _iter_batches(conn, ids, embedder, batch_chunks):
        if deadline is not None and time.monotonic() >= deadline:
            break
        vectors = embedder.embed([text for _, texts in batch for text in texts])
        offset = 0
        for message_id, texts in batch:
            message_vectors = vectors[offset : offset + len(texts)]
            offset += len(texts)
            if _write_message_chunks(conn, message_id, texts, message_vectors):
                processed += 1

    return processed


# ---------------------------------------------------------------------------
# `mail embed --measure N`: the input for decision D1 in
# docs/plans/mail-personal-v1.md (model and host size).
# ---------------------------------------------------------------------------


def current_rss_bytes() -> int:
    """Resident set size of this process right now (Linux /proc), 0 if unknown."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def peak_rss_bytes() -> int:
    import resource

    # ru_maxrss is KiB on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def peak_vram_bytes() -> int | None:
    """torch's peak allocated CUDA memory for this process, None without CUDA (or
    without torch imported at all, e.g. under the test stub embedder).
    """
    import sys

    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return None
    return int(torch.cuda.max_memory_allocated())


def measure(conn, embedder, n: int, batch_chunks: int = BATCH_CHUNKS) -> dict:
    """Embeds the `n` most recent live messages (embedded or not) into a TEMP table and
    reports the rate. Nothing is written to mail.db: the scratch table lives in
    SQLite's temp store and the connection never commits.
    """
    ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM messages WHERE deleted_at IS NULL "
            "ORDER BY internal_date DESC LIMIT ?", (n,),
        )
    ]
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS measure_scratch "
        "(message_id TEXT, idx INTEGER, embedding BLOB)"
    )

    messages = chunks = 0
    started = time.monotonic()
    for batch in _iter_batches(conn, ids, embedder, batch_chunks):
        vectors = embedder.embed([text for _, texts in batch for text in texts])
        offset = 0
        for message_id, texts in batch:
            for idx, vector in enumerate(vectors[offset : offset + len(texts)]):
                conn.execute(
                    "INSERT INTO temp.measure_scratch VALUES (?, ?, ?)",
                    (message_id, idx, sqlite_vec.serialize_float32(vector)),
                )
            offset += len(texts)
            chunks += len(texts)
            messages += 1
    elapsed = time.monotonic() - started
    conn.execute("DROP TABLE temp.measure_scratch")

    per_minute = messages / elapsed * 60 if elapsed > 0 else 0.0
    backlog = conn.execute(
        "SELECT COUNT(*) FROM messages m WHERE m.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.message_id = m.id)"
    ).fetchone()[0]
    return {
        "messages": messages,
        "chunks": chunks,
        "seconds": elapsed,
        "messages_per_minute": per_minute,
        "backlog": backlog,
        "backlog_hours": (backlog / per_minute / 60) if per_minute else None,
    }
