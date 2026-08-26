from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.mail import gmail, store
from tests.mail.fake_gmail import FakeGmailPort, raw_message

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "mail.db")
    yield c
    c.close()


@pytest.fixture
def config(env_dirs):
    from src.mail.config import load_config

    return load_config(env_dirs["data"])  # address: test-owner@example.com


def _seed_default_mailbox(port: FakeGmailPort):
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))
    port.seed(raw_message(FIXTURES / "005-english-plain.eml", id="m005"))
    port.seed(raw_message(FIXTURES / "010-from-owner.eml", id="m010"))


def test_backfill_stores_all_seeded_messages(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)

    result = gmail.sync(conn, port, config)

    assert result.mode == "backfill"
    assert result.done is True
    assert result.fetched == 3
    assert store.count_messages(conn) == 3
    assert gmail.get_sync_state(conn, "backfill_complete") == "1"
    assert gmail.get_sync_state(conn, "history_id") == "1"


def test_backfill_sets_is_from_owner(conn, config):
    import dataclasses

    # 010-from-owner.eml's From: is the fixtures' placeholder identity, not
    # env_dirs' test-owner@example.com — swap the address this one test checks against.
    config = dataclasses.replace(config, address="owner@example.com")
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.sync(conn, port, config)

    assert store.get_message(conn, "m010")["is_from_owner"] == 1
    assert store.get_message(conn, "m001")["is_from_owner"] == 0


def test_backfill_is_resumable_across_a_budget_cutoff(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)

    first = gmail.sync(conn, port, config, budget_seconds=0)
    assert first.mode == "backfill"
    assert first.done is False
    assert first.fetched == 0
    assert store.count_messages(conn) == 0
    pending_count = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    assert pending_count == 3

    second = gmail.sync(conn, port, config)
    assert second.done is True
    assert second.fetched == 3
    assert store.count_messages(conn) == 3
    remaining = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    assert remaining == 0


def test_resuming_a_partially_drained_backfill_does_not_duplicate_or_relist(conn, config):
    """Simulates the state a prior run left behind after hitting its budget mid-drain:
    discovery already ran (history_id recorded, pending populated) but nothing fetched
    yet. A fresh sync() call should drain what's pending without re-listing the mailbox
    or double-counting, and the run after that should be a normal incremental no-op.
    """
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.set_sync_state(conn, "history_id", "1")
    for message_id in ("m001", "m005", "m010"):
        conn.execute(
            "INSERT INTO pending(id, added_at) VALUES (?, datetime('now'))", (message_id,)
        )
    conn.commit()

    result = gmail.sync(conn, port, config)
    assert result.mode == "backfill"
    assert result.done is True
    assert result.fetched == 3
    assert store.count_messages(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0

    result = gmail.sync(conn, port, config)
    assert result.mode == "incremental"
    assert result.fetched == 0


def test_incremental_fetches_new_message_and_marks_deleted(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.sync(conn, port, config)  # complete the backfill first

    new_message = raw_message(FIXTURES / "006-receipt.eml", id="m006")
    port.deliver(new_message)
    port.delete("m001")

    result = gmail.sync(conn, port, config)

    assert result.mode == "incremental"
    assert result.done is True
    assert store.get_message(conn, "m006") is not None
    assert store.get_message(conn, "m001")["deleted_at"] is not None
    assert gmail.get_sync_state(conn, "history_id") == str(port.get_profile()["history_id"])


def test_incremental_no_changes_is_a_no_op(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.sync(conn, port, config)

    result = gmail.sync(conn, port, config)
    assert result.mode == "incremental"
    assert result.fetched == 0
    assert result.done is True


def test_history_expired_falls_back_to_recent_message_list(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.sync(conn, port, config)

    new_message = raw_message(FIXTURES / "004-german-plain.eml", id="m004")
    port.deliver(new_message)
    port.expire_history_before(1_000_000)  # force the stored history_id to look "too old"

    result = gmail.sync(conn, port, config)

    assert result.mode == "incremental-fallback"
    assert store.get_message(conn, "m004") is not None
    # history_id after a fallback comes from get_profile(), not from history.list
    assert gmail.get_sync_state(conn, "history_id") == str(port.get_profile()["history_id"])


def test_deadline_check_helpers():
    assert gmail._make_deadline(None) is None
    assert gmail._deadline_passed(None) is False
    past_deadline = gmail._make_deadline(0)
    assert gmail._deadline_passed(past_deadline) is True


def test_internal_date_to_iso_round_trip():
    dt = datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)
    iso = gmail._internal_date_to_iso(int(dt.timestamp() * 1000))
    assert iso.startswith("2026-08-15T09:00:00")


def test_reset_sync_state_forces_a_fresh_full_backfill(conn, config):
    port = FakeGmailPort()
    _seed_default_mailbox(port)
    gmail.sync(conn, port, config)
    assert gmail.get_sync_state(conn, "backfill_complete") == "1"

    gmail.reset_sync_state(conn)
    assert gmail.get_sync_state(conn, "backfill_complete") is None
    assert gmail.get_sync_state(conn, "history_id") is None
    assert conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0

    result = gmail.sync(conn, port, config)
    assert result.mode == "backfill"
    assert result.done is True
    # existing messages are re-verified via upsert, not duplicated
    assert store.count_messages(conn) == 3


def test_reset_sync_state_clears_stale_pending_rows(conn, config):
    conn.execute("INSERT INTO pending(id, added_at) VALUES ('stale', datetime('now'))")
    gmail.set_sync_state(conn, "history_id", "5")
    gmail.reset_sync_state(conn)
    assert conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0
