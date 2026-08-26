from pathlib import Path

import pytest

from src.mail import search, store

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


@pytest.fixture
def loaded_conn(tmp_path, fixture_ingester):
    conn = store.connect(tmp_path / "mail.db")
    fixture_ingester(conn, FIXTURES / "001-plain-simple.eml", "m001",
                      date_iso="2026-08-15T09:00:00+02:00")
    fixture_ingester(conn, FIXTURES / "005-english-plain.eml", "m005",
                      date_iso="2026-08-15T13:00:00+02:00")
    fixture_ingester(conn, FIXTURES / "004-german-plain.eml", "m004",
                      date_iso="2026-08-15T12:00:00+02:00")
    fixture_ingester(conn, FIXTURES / "006-receipt.eml", "m006",
                      date_iso="2026-07-01T08:00:00+02:00")
    yield conn
    conn.close()


@pytest.fixture
def marker_conn(tmp_path, message_factory):
    """Four synthetic messages sharing the word 'marker' but spread across dates —
    isolates date-filter/limit behaviour from content-matching behaviour (which
    `loaded_conn` covers with real fixtures) without relying on FTS boolean operators,
    since search_fts now ANDs sanitized terms rather than passing the query through.
    """
    conn = store.connect(tmp_path / "mail.db")
    for msg_id, date_iso in [
        ("a1", "2026-06-15T09:00:00+02:00"),
        ("a2", "2026-07-01T08:00:00+02:00"),
        ("a3", "2026-08-15T09:00:00+02:00"),
        ("a4", "2026-08-20T09:00:00+02:00"),
    ]:
        store.upsert_message(
            conn, message_factory(msg_id, subject="marker message", date_iso=date_iso)
        )
    yield conn
    conn.close()


def test_fts_matches_subject(loaded_conn):
    hits = search.search_fts(loaded_conn, "gutter")
    assert [h["id"] for h in hits] == ["m005"]


def test_fts_matches_body_umlauts(loaded_conn):
    hits = search.search_fts(loaded_conn, "bestätigen")
    assert [h["id"] for h in hits] == ["m004"]


def test_fts_subject_ranks_above_body_only_match(loaded_conn):
    # "quote" appears in m005's subject ("Roof gutter quote") and nowhere else here.
    hits = search.search_fts(loaded_conn, "quote")
    assert hits and hits[0]["id"] == "m005"


def test_fts_from_filter(loaded_conn):
    hits = search.search_fts(loaded_conn, "quote", from_filter="hartmann-dachbau.example")
    assert all("hartmann-dachbau.example" in h["from_addr"] for h in hits)
    assert {h["id"] for h in hits} == {"m005"}


def test_fts_since_month_filter(marker_conn):
    hits = search.search_fts(marker_conn, "marker", since="2026-08")
    ids = {h["id"] for h in hits}
    assert ids == {"a3", "a4"}


def test_fts_until_excludes_later_month(marker_conn):
    # until="2026-07" means "up to and including the end of July".
    hits = search.search_fts(marker_conn, "marker", until="2026-07")
    ids = {h["id"] for h in hits}
    assert ids == {"a1", "a2"}


def test_fts_since_and_until_narrow_to_one_day(marker_conn):
    hits = search.search_fts(marker_conn, "marker", since="2026-08-20", until="2026-08-20")
    assert {h["id"] for h in hits} == {"a4"}


def test_fts_respects_limit(marker_conn):
    hits = search.search_fts(marker_conn, "marker", limit=2)
    assert len(hits) <= 2


def test_fts_excludes_deleted(loaded_conn):
    store.mark_deleted(loaded_conn, "m005", "2026-08-20T00:00:00Z")
    hits = search.search_fts(loaded_conn, "gutter")
    assert hits == []


def test_search_dispatches_fts_mode(loaded_conn):
    hits = search.search(loaded_conn, "gutter", mode="fts")
    assert [h["id"] for h in hits] == ["m005"]


def test_search_vec_mode_not_yet_available(loaded_conn):
    with pytest.raises(search.SearchError, match="stage 5"):
        search.search(loaded_conn, "gutter", mode="vec")


def test_invalid_date_filter_raises(loaded_conn):
    with pytest.raises(search.SearchError, match="invalid date filter"):
        search.search_fts(loaded_conn, "gutter", since="not-a-date")


def test_query_with_punctuation_does_not_raise(loaded_conn):
    # foo-bar, a colon, etc. are FTS5 syntax if passed through unquoted.
    hits = search.search_fts(loaded_conn, "invoice: hetzner-example.com")
    assert hits == []


def test_query_with_stray_quote_does_not_raise(loaded_conn):
    hits = search.search_fts(loaded_conn, 'say "hi" now')
    assert hits == []


def test_query_terms_are_anded(loaded_conn):
    # "gutter" alone matches m005; adding an unrelated word narrows to nothing,
    # confirming terms are joined with implicit AND rather than passed through raw.
    assert search.search_fts(loaded_conn, "gutter") != []
    assert search.search_fts(loaded_conn, "gutter nonexistentword") == []


def test_trailing_star_is_prefix_search(loaded_conn):
    hits = search.search_fts(loaded_conn, "gut*")
    assert {h["id"] for h in hits} == {"m005"}
