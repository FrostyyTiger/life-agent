from pathlib import Path

import pytest

from src.mail import cli, gmail, rawstore, store, verify
from tests.mail.fake_gmail import FakeGmailPort, raw_message

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


@pytest.fixture
def config(env_dirs):
    from src.mail.config import load_config

    return load_config(env_dirs["data"])


@pytest.fixture
def db(env_dirs):
    conn = store.connect(env_dirs["state"] / "mail.db")
    yield conn
    conn.close()


@pytest.fixture
def raw_dir(env_dirs):
    return env_dirs["state"] / "raw"


@pytest.fixture
def port():
    p = FakeGmailPort()
    p.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))
    p.seed(raw_message(FIXTURES / "005-english-plain.eml", id="m005"))
    p.seed(raw_message(FIXTURES / "010-from-owner.eml", id="m010"))
    return p


@pytest.fixture
def cli_port(monkeypatch, port):
    monkeypatch.setattr(cli, "_readonly_port", lambda env: port)
    return port


def test_verify_ok_after_a_complete_sync(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    report = verify.verify(db, port, raw_dir)
    assert report.ok
    assert (report.gmail_total, report.rows_live, report.raw_files) == (3, 3, 3)
    assert "OK:" in verify.format_report(report)


def test_verify_catches_a_missing_raw_file(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    rawstore.list_raw_ids(raw_dir)["m005"].unlink()

    report = verify.verify(db, port, raw_dir)
    assert not report.ok
    assert report.rows_without_raw == ["m005"]
    assert "m005" in verify.format_report(report)


def test_verify_catches_an_orphan_raw_file(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    rawstore.write_raw(raw_dir, "orphan1", 0, b"Subject: x\r\n\r\n")

    report = verify.verify(db, port, raw_dir)
    assert not report.ok
    assert report.raw_without_row == ["orphan1"]


def test_verify_catches_a_message_gmail_has_but_the_archive_does_not(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m999"))  # not yet synced

    report = verify.verify(db, port, raw_dir)
    assert not report.ok
    assert report.in_gmail_not_archived == ["m999"]


def test_verify_fails_while_backfill_is_unfinished(db, port, config, raw_dir):
    gmail.sync(db, port, config, budget_seconds=0)
    report = verify.verify(db, port, raw_dir)
    assert not report.ok
    assert report.pending == 3
    assert not report.backfill_complete


def test_deleted_message_keeps_raw_file_and_still_verifies(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    port.delete("m001")
    gmail.sync(db, port, config)

    report = verify.verify(db, port, raw_dir)
    assert report.ok
    assert (report.rows_live, report.rows_deleted, report.raw_files) == (2, 1, 3)


def test_messages_total_mismatch_is_reported_not_fatal(db, port, config, raw_dir, monkeypatch):
    gmail.sync(db, port, config)
    monkeypatch.setattr(port, "get_profile", lambda: {"history_id": "1", "messages_total": 5})
    report = verify.verify(db, port, raw_dir)
    assert report.ok
    assert "messagesTotal (5) != listed ids (3)" in verify.format_report(report)


def test_sync_records_gmail_total_for_status(db, port, config, raw_dir, env_dirs):
    gmail.sync(db, port, config)
    counts = verify.archive_counts(db, raw_dir, env_dirs["state"] / "mail.db")
    assert counts["gmail_total"] == 3
    assert counts["gmail_total_at"]
    assert (counts["rows_live"], counts["raw_files"], counts["unembedded"]) == (3, 3, 3)
    assert counts["disk_bytes"] > 0


def test_backfill_raw_repairs_rows_without_files(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    for path in rawstore.list_raw_ids(raw_dir).values():
        path.unlink()

    result = verify.backfill_raw(db, port, raw_dir)
    assert (result.written, result.unrecoverable, result.remaining) == (3, [], 0)
    assert verify.verify(db, port, raw_dir).ok
    archived = rawstore.list_raw_ids(raw_dir)
    assert rawstore.read_raw(archived["m010"]) == (FIXTURES / "010-from-owner.eml").read_bytes()


def test_backfill_raw_reports_messages_gmail_no_longer_has(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    rawstore.list_raw_ids(raw_dir)["m001"].unlink()
    port.delete("m001")

    result = verify.backfill_raw(db, port, raw_dir)
    assert result.unrecoverable == ["m001"]
    assert store.get_message(db, "m001") is not None  # the row is never dropped


def test_backfill_raw_respects_budget(db, port, config, raw_dir):
    gmail.sync(db, port, config)
    for path in rawstore.list_raw_ids(raw_dir).values():
        path.unlink()
    result = verify.backfill_raw(db, port, raw_dir, budget_seconds=0)
    assert (result.written, result.remaining) == (0, 3)


# --- CLI ---


def test_cli_verify_exit_codes(env_dirs, db, cli_port, config, raw_dir, capsys):
    gmail.sync(db, cli_port, config, raw_dir=raw_dir)
    assert cli.main(["verify"]) == 0
    assert "OK:" in capsys.readouterr().out

    rawstore.list_raw_ids(raw_dir)["m001"].unlink()
    assert cli.main(["verify"]) == 1
    assert "rows without a raw file" in capsys.readouterr().out


def test_cli_verify_without_token_needs_marcel(env_dirs, capsys):
    assert cli.main(["verify"]) == 2
    assert "NEED-MARCEL" in capsys.readouterr().err


def test_cli_backfill_raw(env_dirs, db, cli_port, config, raw_dir, capsys):
    gmail.sync(db, cli_port, config, raw_dir=raw_dir)
    rawstore.list_raw_ids(raw_dir)["m005"].unlink()
    assert cli.main(["backfill-raw"]) == 0
    assert "written=1 unrecoverable=0 remaining=0" in capsys.readouterr().out


def test_cli_status_prints_the_three_counts(env_dirs, db, port, config, raw_dir, capsys):
    gmail.sync(db, port, config)
    port.delete("m001")
    gmail.sync(db, port, config)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "gmail total:      2 (at last sync" in out
    assert "rows:             2 live, 1 deleted in Gmail (kept)" in out
    assert "raw files:        3" in out
    assert "not embedded:     2" in out
    assert "disk:" in out
