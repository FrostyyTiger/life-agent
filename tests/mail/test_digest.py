import json
from datetime import date, datetime, timezone

import pytest

from src.mail import digest, store
from src.mail.config import load_config


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "mail.db")
    yield c
    c.close()


@pytest.fixture
def config(env_dirs):
    return load_config(env_dirs["data"])  # address: test-owner@example.com


def _tagged_message(conn, message_factory, msg_id, *, category, importance=1, summary="s",
                     action=None, deadline=None, tagged_at=None, **overrides):
    # tagged_at must be "recent" relative to the real wall clock, not a fixed date —
    # compose()'s default lookback (no prior digest) is real now() - 24h.
    if tagged_at is None:
        tagged_at = datetime.now(timezone.utc).isoformat()
    store.upsert_message(conn, message_factory(msg_id, **overrides))
    store.upsert_tag(conn, {
        "message_id": msg_id, "category": category, "importance": importance,
        "summary": summary, "action": action, "deadline": deadline,
        "people_json": "[]", "model": "haiku", "attempts": 1,
        "tagged_at": tagged_at, "error": None,
    })


def _fake_runner(response):
    def runner(prompt, *, model, json_schema, conf_dir, state_dir):
        return response
    return runner


def _failing_runner(*a, **kw):
    from src.mail import claude_cli
    raise claude_cli.ClaudeCliError("boom")


# --- bucketing / refs ---

def test_bucket_messages_sorts_into_sections(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)
    _tagged_message(conn, message_factory, "m2", category="needs-you", importance=0)  # too low
    _tagged_message(conn, message_factory, "m3", category="fyi")
    _tagged_message(conn, message_factory, "m4", category="receipt")
    _tagged_message(conn, message_factory, "m5", category="newsletter")
    _tagged_message(conn, message_factory, "m6", category="junk")

    tagged = digest.select_tagged_since(conn, "2026-01-01T00:00:00Z")
    sections = digest.bucket_messages(tagged, config)

    assert [m["id"] for m in sections.needs_you] == ["m1"]
    assert [m["id"] for m in sections.worth_knowing] == ["m3"]
    assert [m["id"] for m in sections.receipts_notifications] == ["m4"]
    assert [m["id"] for m in sections.newsletters] == ["m5"]
    assert [m["id"] for m in sections.junk] == ["m6", "m2"]  # low-importance needs-you falls to junk...


def test_needs_you_capped_at_config_max(conn, message_factory):
    from src.mail.config import MailConfig

    cfg = MailConfig(address="a@example.com", tag_since=date(2026, 1, 1), max_needs_you=2)
    for i in range(5):
        _tagged_message(conn, message_factory, f"m{i}", category="needs-you", importance=3)

    tagged = digest.select_tagged_since(conn, "2026-01-01T00:00:00Z")
    sections = digest.bucket_messages(tagged, cfg)
    assert len(sections.needs_you) == 2


def test_assign_refs_numbers_only_individually_listed_items(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)
    _tagged_message(conn, message_factory, "m2", category="fyi")
    _tagged_message(conn, message_factory, "m3", category="receipt")
    _tagged_message(conn, message_factory, "m4", category="newsletter")  # not numbered
    _tagged_message(conn, message_factory, "m5", category="junk")  # not numbered

    tagged = digest.select_tagged_since(conn, "2026-01-01T00:00:00Z")
    sections = digest.bucket_messages(tagged, config)
    refs = digest.assign_refs(sections)

    assert refs == {"1": "m1", "2": "m2", "3": "m3"}


# --- compose(): degraded mode, subjects-only mode, model success ---

def test_compose_with_no_tags_lists_subjects(conn, config, message_factory):
    store.upsert_message(conn, message_factory(
        "m1", subject="Hello there", fetched_at=datetime.now(timezone.utc).isoformat()
    ))
    markdown, refs_json, degraded = digest.compose(
        conn, config, date(2026, 8, 15), conf_dir=None, state_dir=None,
        claude_runner=_failing_runner,
    )
    assert "Hello there" in markdown
    assert "## New mail" in markdown
    assert degraded is False
    assert refs_json == "{}"


def test_compose_degrades_when_model_call_fails(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3,
                     summary="Quote pending", action="Follow up", deadline="2026-09-01",
                     from_name="Hartmann")

    markdown, refs_json, degraded = digest.compose(
        conn, config, date(2026, 8, 15), conf_dir=None, state_dir=None,
        claude_runner=_failing_runner,
    )

    assert degraded is True
    assert "Degraded" in markdown
    assert "Hartmann" in markdown
    assert "Quote pending" in markdown
    assert json.loads(refs_json) == {"1": "m1"}


def test_compose_uses_model_prose_on_success(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)

    runner = _fake_runner({
        "needs_you_lines": ["Custom model-written line about m1"],
        "worth_knowing_lines": [],
        "while_away": None,
    })
    markdown, refs_json, degraded = digest.compose(
        conn, config, date(2026, 8, 15), conf_dir=None, state_dir=None, claude_runner=runner,
    )

    assert degraded is False
    assert "Custom model-written line about m1" in markdown
    assert "Degraded" not in markdown


def test_compose_degrades_on_line_count_mismatch(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)
    _tagged_message(conn, message_factory, "m2", category="needs-you", importance=3)

    runner = _fake_runner({
        "needs_you_lines": ["only one line for two items"],
        "worth_knowing_lines": [],
        "while_away": None,
    })
    markdown, refs_json, degraded = digest.compose(
        conn, config, date(2026, 8, 15), conf_dir=None, state_dir=None, claude_runner=runner,
    )
    assert degraded is True


def test_compose_acknowledges_pending_reply_feedback(conn, config, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)
    store.add_feedback(conn, message_id="m1", verdict="reply", note="sounds good",
                        source_msg_id="reply-1", created_at="2026-08-14T00:00:00Z")

    markdown, _, _ = digest.compose(
        conn, config, date(2026, 8, 15), conf_dir=None, state_dir=None,
        claude_runner=_failing_runner,
    )
    assert "Noted 1 reply request" in markdown
    assert store.get_unacknowledged_reply_feedback(conn) == []


# --- digest(): file writing, skip-if-exists, insert wiring ---

def test_digest_writes_file_and_records_row(conn, config, env_dirs, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)

    result = digest.digest(
        conn, config, env_dirs["data"], conf_dir=env_dirs["conf"], state_dir=env_dirs["state"],
        target_date=date(2026, 8, 15), claude_runner=_failing_runner,
    )

    assert result.written is True
    assert result.path.exists()
    row = store.get_digest(conn, "2026-08-15")
    assert row is not None
    assert json.loads(row["refs_json"]) == {"1": "m1"}


def test_digest_dry_run_writes_nothing(conn, config, env_dirs, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)

    result = digest.digest(
        conn, config, env_dirs["data"], conf_dir=env_dirs["conf"], state_dir=env_dirs["state"],
        target_date=date(2026, 8, 15), dry_run=True, claude_runner=_failing_runner,
    )
    assert result.written is False
    assert not result.path.exists()
    assert store.get_digest(conn, "2026-08-15") is None


def test_digest_never_overwrites_an_existing_brief(conn, config, env_dirs, message_factory):
    briefs_dir = env_dirs["data"] / "briefs"
    briefs_dir.mkdir(parents=True)
    existing = briefs_dir / "2026-08-15-mail.md"
    existing.write_text("original content, must survive")

    result = digest.digest(
        conn, config, env_dirs["data"], conf_dir=env_dirs["conf"], state_dir=env_dirs["state"],
        target_date=date(2026, 8, 15), claude_runner=_failing_runner,
    )

    assert result.skipped_existing is True
    assert existing.read_text() == "original content, must survive"


def test_digest_insert_success_records_message_id(conn, config, env_dirs, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)

    inserted_ids = []

    def insert_fn(raw_bytes):
        inserted_ids.append(raw_bytes)
        return "gmail-msg-id-123"

    result = digest.digest(
        conn, config, env_dirs["data"], conf_dir=env_dirs["conf"], state_dir=env_dirs["state"],
        target_date=date(2026, 8, 15), claude_runner=_failing_runner, insert_fn=insert_fn,
    )

    assert result.inserted is True
    assert result.insert_error is None
    assert len(inserted_ids) == 1
    row = store.get_digest(conn, "2026-08-15")
    assert row["inserted_gmail_id"] == "gmail-msg-id-123"
    assert row["message_id_hdr"].startswith("<digest-20260815-")


def test_digest_insert_failure_does_not_fail_the_whole_run(conn, config, env_dirs, message_factory):
    _tagged_message(conn, message_factory, "m1", category="needs-you", importance=3)

    def insert_fn(raw_bytes):
        raise RuntimeError("Gmail is down")

    result = digest.digest(
        conn, config, env_dirs["data"], conf_dir=env_dirs["conf"], state_dir=env_dirs["state"],
        target_date=date(2026, 8, 15), claude_runner=_failing_runner, insert_fn=insert_fn,
    )

    assert result.written is True  # the file exists — this is what satisfies the deadman's switch
    assert result.inserted is False
    assert "Gmail is down" in result.insert_error


def test_build_email_uses_constant_recipient_never_model_output(config):
    raw_bytes, message_id = digest._build_email(config, date(2026, 8, 15), "some markdown")
    import email as email_mod

    msg = email_mod.message_from_bytes(raw_bytes, policy=email_mod.policy.default)
    assert msg["To"] == config.address
    assert config.address in msg["From"]
    assert message_id.startswith("<digest-20260815-")
    assert message_id.endswith("@life-agent>")
