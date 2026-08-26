"""A deterministic bag-of-hashed-words embedder — no model, no download. Same words
produce similar vectors, which is enough to validate KNN plumbing (candidate retrieval,
message-level grouping, filters, hybrid fusion) without needing real semantic
understanding. Vector width matches store.EMBEDDING_DIMENSIONS so it's a drop-in for
the real sqlite-vec schema.
"""

from __future__ import annotations

import math

from src.mail import store

DIMENSIONS = store.EMBEDDING_DIMENSIONS


class FakeEmbedder:
    def chunk(self, text: str, max_chunks: int = 8) -> list[str]:
        if not text.strip():
            return []
        words = text.split()
        size = 40
        chunks = [" ".join(words[i : i + size]) for i in range(0, len(words), size)]
        return chunks[:max_chunks]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in text.lower().split():
            vector[hash(word) % DIMENSIONS] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]
