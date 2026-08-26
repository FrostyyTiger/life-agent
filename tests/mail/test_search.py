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
    hits = search.search_fts(loaded_conn, "Owner OR Thursday OR gutter OR quote",
                              from_filter="hartmann-dachbau.example")
    assert all("hartmann-dachbau.example" in h["from_addr"] for h in hits)
    assert any(h["id"] == "m005" for h in hits)


def test_fts_since_until_month_filter(loaded_conn):
    hits = search.search_fts(loaded_conn, "receipt OR gutter OR bestätigen OR Thursday",
                              since="2026-08")
    ids = {h["id"] for h in hits}
    assert "m006" not in ids  # July, excluded
    assert {"m001", "m004", "m005"} <= ids


def test_fts_until_excludes_later_month(loaded_conn):
    # until="2026-07" means "up to and including the end of July" — only the July receipt.
    hits = search.search_fts(loaded_conn, "receipt OR gutter OR bestätigen OR Thursday",
                              until="2026-07")
    ids = {h["id"] for h in hits}
    assert ids == {"m006"}


def test_fts_respects_limit(loaded_conn):
    hits = search.search_fts(loaded_conn, "Owner OR Thursday OR gutter OR bestätigen OR receipt",
                              limit=2)
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
