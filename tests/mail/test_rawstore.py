import gzip
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.mail import gmail, rawstore, store
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

    return load_config(env_dirs["data"])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_raw_path_uses_utc_month_of_internal_date(tmp_path):
    # 2026-08-31T23:30 at UTC+2 is still August in UTC; 2026-09-01T00:30Z is September.
    late_aug = int(datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc).timestamp() * 1000)
    early_sep = int(datetime(2026, 9, 1, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert rawstore.raw_path(tmp_path, "abc", late_aug) == tmp_path / "2026" / "08" / "abc.eml.gz"
    assert rawstore.raw_path(tmp_path, "abc", early_sep) == tmp_path / "2026" / "09" / "abc.eml.gz"


@pytest.mark.parametrize("bad_id", ["../x", "a/b", "", ".hidden", "a b"])
def test_raw_path_refuses_unsafe_ids(tmp_path, bad_id):
    with pytest.raises(ValueError):
        rawstore.raw_path(tmp_path, bad_id, 0)


def test_write_raw_is_gzip_of_exact_bytes_with_private_modes(tmp_path):
    raw_dir = tmp_path / "raw"
    data = (FIXTURES / "001-plain-simple.eml").read_bytes()
    old_umask = os.umask(0o022)  # a permissive umask must not leak into the modes
    try:
        assert rawstore.write_raw(raw_dir, "m1", 1786777200000, data) is True
    finally:
        os.umask(old_umask)

    path = rawstore.raw_path(raw_dir, "m1", 1786777200000)
    with gzip.open(path, "rb") as fh:
        assert fh.read() == data
    assert _mode(path) == 0o600
    for directory in (raw_dir, path.parent.parent, path.parent):
        assert _mode(directory) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["m1.eml.gz"]  # no temp files left


def test_write_raw_same_bytes_is_a_no_op(tmp_path):
    raw_dir = tmp_path / "raw"
    data = b"Subject: hi\r\n\r\nbody\r\n"
    rawstore.write_raw(raw_dir, "m1", 0, data)
    path = rawstore.raw_path(raw_dir, "m1", 0)
    before = path.stat()

    assert rawstore.write_raw(raw_dir, "m1", 0, data) is False
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_write_raw_never_overwrites_different_bytes(tmp_path, capsys):
    raw_dir = tmp_path / "raw"
    rawstore.write_raw(raw_dir, "m1", 0, b"original")
    assert rawstore.write_raw(raw_dir, "m1", 0, b"changed") is False
    assert rawstore.read_raw(rawstore.raw_path(raw_dir, "m1", 0)) == b"original"
    assert "different bytes" in capsys.readouterr().err


def test_sync_archives_every_message_next_to_the_db(conn, config, tmp_path):
    port = FakeGmailPort()
    for name, mid in (("001-plain-simple.eml", "m001"), ("005-english-plain.eml", "m005")):
        port.seed(raw_message(FIXTURES / name, id=mid))

    gmail.sync(conn, port, config)

    raw_dir = tmp_path / "raw"
    archived = rawstore.list_raw_ids(raw_dir)
    assert set(archived) == {"m001", "m005"}
    assert rawstore.read_raw(archived["m005"]) == (FIXTURES / "005-english-plain.eml").read_bytes()


def test_resync_leaves_raw_files_alone(conn, config, tmp_path):
    port = FakeGmailPort()
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))
    gmail.sync(conn, port, config)
    path = rawstore.list_raw_ids(tmp_path / "raw")["m001"]
    before = path.stat()

    gmail.reset_sync_state(conn)  # `mail sync --full`: refetches everything
    gmail.sync(conn, port, config)

    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_gmail_deletion_keeps_the_raw_file(conn, config, tmp_path):
    port = FakeGmailPort()
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))
    gmail.sync(conn, port, config)

    port.delete("m001")
    gmail.sync(conn, port, config)

    assert store.get_message(conn, "m001")["deleted_at"] is not None
    assert "m001" in rawstore.list_raw_ids(tmp_path / "raw")


def test_explicit_raw_dir_is_used(conn, config, tmp_path):
    port = FakeGmailPort()
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))
    elsewhere = tmp_path / "elsewhere"
    gmail.sync(conn, port, config, raw_dir=elsewhere)
    assert set(rawstore.list_raw_ids(elsewhere)) == {"m001"}
    assert not (tmp_path / "raw").exists()


def test_raw_file_is_written_before_the_row(conn, config, tmp_path, monkeypatch):
    port = FakeGmailPort()
    port.seed(raw_message(FIXTURES / "001-plain-simple.eml", id="m001"))

    def boom(*args, **kwargs):
        raise RuntimeError("crash after the raw write")

    monkeypatch.setattr(store, "upsert_message", boom)
    with pytest.raises(RuntimeError):
        gmail.sync(conn, port, config)

    assert "m001" in rawstore.list_raw_ids(tmp_path / "raw")
    pending = conn.execute("SELECT id FROM pending").fetchall()
    assert [r["id"] for r in pending] == ["m001"]  # the next run stores the row
