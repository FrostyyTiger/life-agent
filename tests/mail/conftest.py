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
        "internal_date": 1755248400000,
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
