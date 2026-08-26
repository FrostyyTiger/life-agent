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
            logger.warning(
                "CUDA not available — running %s on CPU; this will be slow", MODEL_NAME
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
        vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
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


def _messages_needing_embeddings(conn, cutoff_ms: int):
    """Newest-first within the last `RECENT_DAYS_FIRST` days, then oldest-first for
    everything older — recent mail becomes searchable fast; the long tail backfills
    in a stable order.
    """
    recent = conn.execute(
        "SELECT m.* FROM messages m LEFT JOIN chunks c ON c.message_id = m.id "
        "WHERE c.id IS NULL AND m.deleted_at IS NULL AND m.internal_date >= ? "
        "GROUP BY m.id ORDER BY m.internal_date DESC",
        (cutoff_ms,),
    ).fetchall()
    older = conn.execute(
        "SELECT m.* FROM messages m LEFT JOIN chunks c ON c.message_id = m.id "
        "WHERE c.id IS NULL AND m.deleted_at IS NULL AND m.internal_date < ? "
        "GROUP BY m.id ORDER BY m.internal_date ASC",
        (cutoff_ms,),
    ).fetchall()
    return list(recent) + list(older)


def embed_pending(conn, embedder, budget_seconds: float | None = None) -> int:
    """Embed every message that has no chunks yet. Returns the number of messages
    processed. Safe to interrupt and re-run: a message with any chunks is skipped.
    """
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    cutoff_ms = int((time.time() - RECENT_DAYS_FIRST * 86400) * 1000)

    processed = 0
    for message in _messages_needing_embeddings(conn, cutoff_ms):
        if deadline is not None and time.monotonic() >= deadline:
            break

        chunk_texts = build_chunk_texts(dict(message), embedder)
        vectors = embedder.embed(chunk_texts)

        for idx, (text, vector) in enumerate(zip(chunk_texts, vectors)):
            chunk_id = conn.execute(
                "INSERT INTO chunks(message_id, idx, text) VALUES (?, ?, ?)",
                (message["id"], idx, text),
            ).lastrowid
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(vector)),
            )
        conn.commit()
        processed += 1

    return processed
