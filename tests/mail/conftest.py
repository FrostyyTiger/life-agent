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
