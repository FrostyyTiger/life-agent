from datetime import date

import pytest

from src.mail.config import ConfigError, load_config, load_env


def test_load_env_requires_all_three(no_env):
    with pytest.raises(ConfigError, match="LIFE_AGENT_DATA"):
        load_env()


def test_load_env_reports_each_missing_var(env_dirs, monkeypatch):
    monkeypatch.delenv("LIFE_AGENT_CONF")
    with pytest.raises(ConfigError, match="LIFE_AGENT_CONF"):
        load_env()


def test_load_env_succeeds(env_dirs):
    env = load_env()
    assert env.data_dir == env_dirs["data"]
    assert env.state_dir == env_dirs["state"]
    assert env.conf_dir == env_dirs["conf"]


def test_load_config_reads_required_fields(env_dirs):
    config = load_config(env_dirs["data"])
    assert config.address == "test-owner@example.com"
    assert config.tag_since == date(2026, 1, 1)


def test_load_config_applies_defaults(env_dirs):
    config = load_config(env_dirs["data"])
    assert config.max_needs_you == 5
    assert config.body_chars_for_model == 4000
    assert config.vip_senders == []
    assert config.digest_time == "06:30"
    assert config.timezone == "Europe/Zurich"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="no config.yaml"):
        load_config(tmp_path)


def test_load_config_missing_mail_section(tmp_path):
    (tmp_path / "config.yaml").write_text("calendar_id: primary\n")
    with pytest.raises(ConfigError, match="mail"):
        load_config(tmp_path)


def test_load_config_missing_required_field(tmp_path):
    (tmp_path / "config.yaml").write_text("mail:\n  address: x@example.com\n")
    with pytest.raises(ConfigError, match="tag_since"):
        load_config(tmp_path)


def test_load_config_bad_tag_since(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "mail:\n  address: x@example.com\n  tag_since: not-a-date\n"
    )
    with pytest.raises(ConfigError, match="ISO date"):
        load_config(tmp_path)
