"""Search over the archive: `--mode fts` (FTS5 bm25()), `--mode vec` (sqlite-vec KNN
over chunk embeddings), `--mode hybrid` (reciprocal rank fusion of the two, the
default once embeddings exist).
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, timedelta

import sqlite_vec

DEFAULT_LIMIT = 20

# How many nearest chunks to pull per requested result before collapsing to unique
# messages — several chunks per message means the raw KNN k must exceed the message
# limit by a comfortable margin, or a chatty message could crowd out everything else.
VEC_CANDIDATE_MULTIPLIER = 8
VEC_MIN_CANDIDATES = 40

# Reciprocal rank fusion constant — the standard choice, dampens the first few ranks'
# dominance without needing either ranking's raw scores to be comparable.
RRF_K = 60

_TOKEN_RE = re.compile(r"\S+")

# Column order matches messages_fts's CREATE VIRTUAL TABLE: subject, from_addr, body_text.
# Subject weighted well above the other two so a subject-line match outranks an
# equally-frequent body match.
_BM25_WEIGHTS = (5.0, 1.0, 1.0)


class SearchError(Exception):
    pass


def _normalize_date(value: str, *, is_until: bool) -> str:
    """YYYY-MM or YYYY-MM-DD -> an ISO date usable as an inclusive/exclusive bound.

    `since` is the first instant of the given day/month. `until` is exclusive, so it
    normalizes to the instant *after* the given day, or after the given month's last day.
    """
    parts = value.split("-")
    try:
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            if is_until:
                last_day = calendar.monthrange(year, month)[1]
                d = date(year, month, last_day) + timedelta(days=1)
            else:
                d = date(year, month, 1)
        elif len(parts) == 3:
            year, month, day = (int(p) for p in parts)
            d = date(year, month, day)
            if is_until:
                d += timedelta(days=1)
        else:
            raise ValueError
    except ValueError as exc:
        raise SearchError(
            f"invalid date filter {value!r} (expected YYYY-MM or YYYY-MM-DD)"
        ) from exc
    return d.isoformat()


def _quote_term(term: str) -> str:
    """One user-typed word -> one safe FTS5 string literal.

    Quoting every term (rather than passing the query straight to MATCH) is what
    keeps punctuation the user typed as data — a hyphen, a colon, a stray quote —
    from being parsed as FTS5 query syntax. A trailing `*` still means prefix search;
    it survives outside the quotes, which FTS5's grammar allows after a quoted string.
    """
    is_prefix = term.endswith("*") and len(term) > 1
    if is_prefix:
        term = term[:-1]
    quoted = '"' + term.replace('"', '""') + '"'
    return quoted + "*" if is_prefix else quoted


def _sanitize_query(raw: str) -> str:
    """Free-text user input -> an FTS5 MATCH expression, terms implicitly ANDed."""
    terms = _TOKEN_RE.findall(raw)
    if not terms:
        raise SearchError("empty search query")
    return " ".join(_quote_term(term) for term in terms)


def search_fts(
    conn,
    query: str,
    *,
    from_filter: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    sql = [
        f"SELECT m.*, bm25(messages_fts, {_BM25_WEIGHTS[0]}, {_BM25_WEIGHTS[1]}, "
        f"{_BM25_WEIGHTS[2]}) AS rank",
        "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid",
        "WHERE messages_fts MATCH ? AND m.deleted_at IS NULL",
    ]
    params: list = [_sanitize_query(query)]

    if from_filter:
        sql.append("AND m.from_addr LIKE ?")
        params.append(f"%{from_filter}%")
    if since:
        sql.append("AND m.date_iso >= ?")
        params.append(_normalize_date(since, is_until=False))
    if until:
        sql.append("AND m.date_iso < ?")
        params.append(_normalize_date(until, is_until=True))

    sql.append("ORDER BY rank LIMIT ?")
    params.append(limit)

    try:
        rows = conn.execute(" ".join(sql), params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchError(f"invalid search query: {exc}") from exc
    return [dict(row) for row in rows]


def _apply_message_filters(sql: list[str], params: list, from_filter, since, until) -> None:
    if from_filter:
        sql.append("AND from_addr LIKE ?")
        params.append(f"%{from_filter}%")
    if since:
        sql.append("AND date_iso >= ?")
        params.append(_normalize_date(since, is_until=False))
    if until:
        sql.append("AND date_iso < ?")
        params.append(_normalize_date(until, is_until=True))


def search_vec(
    conn,
    query: str,
    embedder,
    *,
    from_filter: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    query_vector = embedder.embed([query])[0]
    k = max(limit * VEC_CANDIDATE_MULTIPLIER, VEC_MIN_CANDIDATES)

    candidates = conn.execute(
        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ?",
        (sqlite_vec.serialize_float32(query_vector), k),
    ).fetchall()
    if not candidates:
        return []

    distance_by_chunk = {row["rowid"]: row["distance"] for row in candidates}
    placeholders = ",".join("?" for _ in distance_by_chunk)
    chunk_rows = conn.execute(
        f"SELECT id, message_id FROM chunks WHERE id IN ({placeholders})",
        list(distance_by_chunk),
    ).fetchall()

    best_distance: dict[str, float] = {}
    for row in chunk_rows:
        d = distance_by_chunk[row["id"]]
        if row["message_id"] not in best_distance or d < best_distance[row["message_id"]]:
            best_distance[row["message_id"]] = d
    if not best_distance:
        return []

    message_ids = list(best_distance)
    placeholders = ",".join("?" for _ in message_ids)
    sql = [f"SELECT * FROM messages WHERE id IN ({placeholders}) AND deleted_at IS NULL"]
    params: list = list(message_ids)
    _apply_message_filters(sql, params, from_filter, since, until)

    rows = [dict(row) for row in conn.execute(" ".join(sql), params).fetchall()]
    rows.sort(key=lambda row: best_distance[row["id"]])
    return rows[:limit]


def search_hybrid(
    conn,
    query: str,
    embedder,
    *,
    from_filter: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    # Over-fetch each side so fusion has enough of both rankings to work with.
    fetch_limit = max(limit * 4, DEFAULT_LIMIT)
    fts_results = search_fts(
        conn, query, from_filter=from_filter, since=since, until=until, limit=fetch_limit
    )
    vec_results = search_vec(
        conn, query, embedder, from_filter=from_filter, since=since, until=until,
        limit=fetch_limit,
    )

    scores: dict[str, float] = {}
    rows_by_id: dict[str, dict] = {}
    for ranking in (fts_results, vec_results):
        for rank, row in enumerate(ranking):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            rows_by_id.setdefault(row["id"], row)

    ordered_ids = sorted(scores, key=lambda message_id: -scores[message_id])[:limit]
    return [rows_by_id[message_id] for message_id in ordered_ids]


def search(conn, query: str, *, mode: str = "fts", embedder=None, **kwargs) -> list[dict]:
    if mode not in ("fts", "vec", "hybrid"):
        raise SearchError(f"unknown search mode {mode!r}")
    if mode == "fts":
        return search_fts(conn, query, **kwargs)
    if embedder is None:
        raise SearchError(f"search mode {mode!r} requires an embedder")
    if mode == "vec":
        return search_vec(conn, query, embedder, **kwargs)
    return search_hybrid(conn, query, embedder, **kwargs)
