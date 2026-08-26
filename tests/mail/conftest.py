import json
import os
import textwrap
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """A throwaway LIFE_AGENT_DATA/STATE/CONF triplet with a minimal config.yaml.

    Never points at the real ~/life-agent-data — tests must not be able to touch it.
    """
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "state"
    conf_dir = tmp_path / "conf"
    data_dir.mkdir()
    state_dir.mkdir()
    conf_dir.mkdir()

    (data_dir / "config.yaml").write_text(
        textwrap.dedent(
            """\
            mail:
              address: test-owner@example.com
              tag_since: 2026-01-01
            """
        )
    )

    monkeypatch.setenv("LIFE_AGENT_DATA", str(data_dir))
    monkeypatch.setenv("LIFE_AGENT_STATE", str(state_dir))
    monkeypatch.setenv("LIFE_AGENT_CONF", str(conf_dir))

    return {"data": data_dir, "state": state_dir, "conf": conf_dir}


@pytest.fixture
def no_env(monkeypatch):
    for var in ("LIFE_AGENT_DATA", "LIFE_AGENT_STATE", "LIFE_AGENT_CONF"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fixture_mail_files():
    return sorted(FIXTURES_DIR.glob("*.eml"))


def make_message(id: str, **overrides) -> dict:
    """A minimal-but-complete `messages` row, for tests that don't care about most fields."""
    message = {
        "id": id,
        "thread_id": f"thread-{id}",
        "history_id": "1",
        "internal_date": 1786777200000,  # 2026-08-15T09:00:00+02:00 — must match date_iso below
        "date_iso": "2026-08-15T09:00:00+02:00",
        "from_addr": "sender@example.com",
        "from_name": "Sender Example",
        "to_addrs": "test-owner@example.com",
        "cc_addrs": None,
        "reply_to": None,
        "message_id_hdr": f"<{id}@fixtures.example>",
        "in_reply_to": None,
        "references_hdr": None,
        "subject": "Test subject",
        "snippet": "Test snippet",
        "body_text": "Test body",
        "labels_json": "[]",
        "has_attachments": 0,
        "attachments_json": "[]",
        "size": 100,
        "is_from_owner": 0,
        "fetched_at": "2026-08-15T09:05:00Z",
    }
    message.update(overrides)
    return message


@pytest.fixture
def message_factory():
    return make_message


def ingest_fixture(conn, path: Path, id: str, **overrides) -> dict:
    """Extract a real .eml fixture and store it, filling in the Gmail-side fields
    (id, thread_id, internal_date, ...) that only gmail.py (stage 3) supplies for real.
    """
    from src.mail import extract, store

    fields = extract.build_message_fields(path.read_bytes())
    message = make_message(id, **fields)
    message.update(overrides)
    store.upsert_message(conn, message)
    return message


@pytest.fixture
def fixture_ingester():
    return ingest_fixture


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Puts a fake `claude` executable on PATH so claude_cli.py's real subprocess
    invocation gets exercised end to end, per the plan's "fake claude binary on PATH"
    verification note — rather than mocking claude_cli at the Python level, which
    would test nothing about the actual command line / env / stdin contract.

    Returns a `configure(...)` callable:
      configure(response={"tags": [...]})   -> envelope wraps the JSON-encoded response
      configure(raw_stdout="not json")       -> stdout is exactly this string
      configure(response=..., exit_code=1)   -> claude exits non-zero
      configure(is_error=True, result="...") -> an is_error envelope
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "response_file = os.environ.get('FAKE_CLAUDE_RESPONSE_FILE')\n"
        "exit_code = int(os.environ.get('FAKE_CLAUDE_EXIT_CODE', '0'))\n"
        "if response_file:\n"
        "    sys.stdout.write(open(response_file).read())\n"
        "sys.exit(exit_code)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    response_path = tmp_path / "fake_claude_response.json"

    def configure(response=None, exit_code=0, raw_stdout=None, is_error=False, result=None):
        if raw_stdout is not None:
            stdout = raw_stdout
        elif is_error:
            stdout = json.dumps({"is_error": True, "result": result})
        else:
            stdout = json.dumps({"is_error": False, "result": json.dumps(response)})
        response_path.write_text(stdout)
        monkeypatch.setenv("FAKE_CLAUDE_RESPONSE_FILE", str(response_path))
        monkeypatch.setenv("FAKE_CLAUDE_EXIT_CODE", str(exit_code))

    return configure
