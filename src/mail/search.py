"""Search over the archive. `--mode fts` (this stage) is full-text via FTS5's bm25();
`--mode vec` and `--mode hybrid` land in stage 5 once embeddings exist.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, timedelta

DEFAULT_LIMIT = 20

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


def search(conn, query: str, *, mode: str = "fts", **kwargs) -> list[dict]:
    if mode != "fts":
        raise SearchError(f"search mode {mode!r} is not available until stage 5 (embeddings)")
    return search_fts(conn, query, **kwargs)
