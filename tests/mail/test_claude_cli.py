import pytest

from src.mail import claude_cli

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@pytest.fixture
def conf_dir(tmp_path):
    d = tmp_path / "conf"
    d.mkdir()
    (d / "claude-oauth-token").write_text("fake-token-value\n")
    return d


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_missing_token_raises_need_marcel_shaped_error(tmp_path, state_dir):
    empty_conf = tmp_path / "empty-conf"
    empty_conf.mkdir()
    with pytest.raises(claude_cli.ClaudeCliError, match="claude setup-token"):
        claude_cli.run(
            "prompt", model="haiku", json_schema=SCHEMA, conf_dir=empty_conf,
            state_dir=state_dir,
        )


def test_successful_round_trip(fake_claude, conf_dir, state_dir):
    fake_claude(response={"ok": True})
    result = claude_cli.run(
        "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir, state_dir=state_dir
    )
    assert result == {"ok": True}


def test_nonzero_exit_raises(fake_claude, conf_dir, state_dir):
    fake_claude(response={"ok": True}, exit_code=1)
    with pytest.raises(claude_cli.ClaudeCliError, match="exited 1"):
        claude_cli.run(
            "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir,
            state_dir=state_dir,
        )


def test_malformed_stdout_raises(fake_claude, conf_dir, state_dir):
    fake_claude(raw_stdout="not json at all")
    with pytest.raises(claude_cli.ClaudeCliError, match="invalid JSON"):
        claude_cli.run(
            "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir,
            state_dir=state_dir,
        )


def test_is_error_envelope_raises(fake_claude, conf_dir, state_dir):
    fake_claude(is_error=True, result="Not logged in")
    with pytest.raises(claude_cli.ClaudeCliError, match="Not logged in"):
        claude_cli.run(
            "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir,
            state_dir=state_dir,
        )


def test_result_already_a_dict_is_accepted(fake_claude, conf_dir, state_dir):
    # In case the real CLI's --json-schema result ever arrives as a nested object
    # rather than a JSON string, that shape should also work without code changes.
    import json

    fake_claude(raw_stdout=json.dumps({"is_error": False, "result": {"ok": True}}))
    result = claude_cli.run(
        "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir, state_dir=state_dir
    )
    assert result == {"ok": True}


def test_uses_bare_binary_name_not_a_hardcoded_path():
    assert claude_cli.CLAUDE_BINARY == "claude"


def test_token_never_appears_in_command_line(fake_claude, conf_dir, state_dir, monkeypatch):
    """The token must travel only via CLAUDE_CODE_OAUTH_TOKEN, never argv — argv is
    visible to any local user via `ps`.
    """
    captured = {}
    fake_claude(response={"ok": True})

    import subprocess as subprocess_mod

    real_run = subprocess_mod.run

    def spy(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess_mod, "run", spy)

    claude_cli.run(
        "prompt", model="haiku", json_schema=SCHEMA, conf_dir=conf_dir, state_dir=state_dir
    )

    assert "fake-token-value" not in captured["command"]
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-token-value"
