import json
from pathlib import Path

from src.mail import cli, store

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


def test_status_refuses_without_env(no_env, capsys):
    exit_code = cli.main(["status"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "LIFE_AGENT_DATA" in err
    assert "LIFE_AGENT_STATE" in err
    assert "LIFE_AGENT_CONF" in err


def test_status_works_with_empty_db(env_dirs, capsys):
    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "test-owner@example.com" in out
    assert "2026-01-01" in out
    assert "messages:         0 (database just created" in out


def test_status_counts_existing_messages(env_dirs, capsys, message_factory):
    from src.mail import store

    db_path = env_dirs["state"] / "mail.db"
    conn = store.connect(db_path)
    for msg_id in ("a", "b", "c"):
        store.upsert_message(conn, message_factory(msg_id))
    conn.close()

    exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "messages:         3" in out


def test_no_command_errors(no_env):
    try:
        cli.main([])
        assert False, "expected SystemExit from argparse (required subcommand)"
    except SystemExit as exc:
        assert exc.code != 0


def test_search_finds_and_prints_a_hit(env_dirs, capsys, fixture_ingester):
    conn = store.connect(env_dirs["state"] / "mail.db")
    fixture_ingester(conn, FIXTURES / "005-english-plain.eml", "m005")
    conn.close()

    exit_code = cli.main(["search", "gutter"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "m005" in out
    assert "Roof gutter quote" in out


def test_search_no_results(env_dirs, capsys):
    exit_code = cli.main(["search", "nonexistent"])
    assert exit_code == 0
    assert "no results" in capsys.readouterr().out


def test_search_json_output(env_dirs, capsys, fixture_ingester):
    conn = store.connect(env_dirs["state"] / "mail.db")
    fixture_ingester(conn, FIXTURES / "005-english-plain.eml", "m005")
    conn.close()

    exit_code = cli.main(["search", "gutter", "--json"])
    assert exit_code == 0
    hits = json.loads(capsys.readouterr().out)
    assert hits[0]["id"] == "m005"


def test_search_vec_mode_without_embedder_reports_error(env_dirs, capsys, monkeypatch):
    # _build_embedder would otherwise construct the real SentenceTransformerEmbedder
    # and download BAAI/bge-m3 — force the "misconfigured" path instead.
    monkeypatch.setattr(cli, "_build_embedder", lambda env: None)
    exit_code = cli.main(["search", "gutter", "--mode", "vec"])
    assert exit_code == 2
    assert "requires an embedder" in capsys.readouterr().err


def test_show_prints_headers_and_body(env_dirs, capsys, fixture_ingester):
    conn = store.connect(env_dirs["state"] / "mail.db")
    fixture_ingester(conn, FIXTURES / "005-english-plain.eml", "m005")
    conn.close()

    exit_code = cli.main(["show", "m005"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Roof gutter quote" in out
    assert "Hartmann Dachbau" in out


def test_show_missing_id(env_dirs, capsys):
    exit_code = cli.main(["show", "nope"])
    assert exit_code == 1
    assert "no message" in capsys.readouterr().err


def test_show_json_output(env_dirs, capsys, fixture_ingester):
    conn = store.connect(env_dirs["state"] / "mail.db")
    fixture_ingester(conn, FIXTURES / "005-english-plain.eml", "m005")
    conn.close()

    exit_code = cli.main(["show", "m005", "--json"])
    assert exit_code == 0
    row = json.loads(capsys.readouterr().out)
    assert row["id"] == "m005"


def test_auth_without_client_secrets_prints_need_marcel(env_dirs, capsys):
    exit_code = cli.main(["auth", "readonly"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert err.startswith("NEED-MARCEL:")
    assert "google-client.json" in err


def test_sync_without_token_prints_need_marcel(env_dirs, capsys):
    exit_code = cli.main(["sync"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert err.startswith("NEED-MARCEL:")
    assert "mail auth" in err


def test_embed_processes_pending_messages(env_dirs, capsys, message_factory, monkeypatch):
    from tests.mail.fake_embedder import FakeEmbedder

    monkeypatch.setattr(cli, "_build_embedder", lambda env: FakeEmbedder())

    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(conn, message_factory("m1", subject="hello", body_text="world"))
    conn.close()

    exit_code = cli.main(["embed"])
    assert exit_code == 0
    assert "processed=1" in capsys.readouterr().out


def test_search_vec_mode_uses_embedder(env_dirs, capsys, message_factory, monkeypatch):
    from src.mail import embed as embed_mod
    from tests.mail.fake_embedder import FakeEmbedder

    monkeypatch.setattr(cli, "_build_embedder", lambda env: FakeEmbedder())

    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(
        conn, message_factory("m1", subject="Roof gutter quote", body_text="Hartmann Dachbau")
    )
    embed_mod.embed_pending(conn, FakeEmbedder())
    conn.close()

    exit_code = cli.main(["search", "gutter quote", "--mode", "vec"])
    assert exit_code == 0
    assert "m1" in capsys.readouterr().out


def test_tag_without_token_fails_loudly_not_need_marcel(env_dirs, capsys, message_factory):
    # mail tag has no NEED-MARCEL short-circuit like auth/sync — it just tries to run
    # claude and claude_cli.py's own missing-token error surfaces.
    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(conn, message_factory("m1"))
    conn.close()

    exit_code = cli.main(["tag"])
    assert exit_code == 0  # tag() itself doesn't fail the whole command
    out = capsys.readouterr().out
    assert "failed=1" in out


def test_tag_uses_fake_claude_binary(env_dirs, capsys, message_factory, fake_claude):
    conf_dir = env_dirs["conf"]
    (conf_dir / "claude-oauth-token").write_text("fake-token\n")

    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(conn, message_factory("m1", subject="Roof gutter quote"))
    conn.close()

    fake_claude(response={"tags": [
        {"id": "m1", "category": "needs-you", "importance": 2, "summary": "s",
         "action": None, "deadline": None, "people": []},
    ]})

    exit_code = cli.main(["tag"])
    assert exit_code == 0
    assert "tagged=1" in capsys.readouterr().out


def test_digest_dry_run_does_not_write_a_file(env_dirs, capsys, message_factory):
    from datetime import datetime, timezone

    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(conn, message_factory(
        "m1", subject="hi", fetched_at=datetime.now(timezone.utc).isoformat()
    ))
    conn.close()

    exit_code = cli.main(["digest", "--dry-run", "--date", "2026-08-15"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "written=False" in out
    assert not (env_dirs["data"] / "briefs" / "2026-08-15-mail.md").exists()


def test_digest_writes_file_without_insert_token(env_dirs, capsys, message_factory):
    from datetime import datetime, timezone

    conn = store.connect(env_dirs["state"] / "mail.db")
    store.upsert_message(conn, message_factory(
        "m1", subject="hi", fetched_at=datetime.now(timezone.utc).isoformat()
    ))
    conn.close()

    exit_code = cli.main(["digest", "--date", "2026-08-15"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "written=True" in captured.out
    assert (env_dirs["data"] / "briefs" / "2026-08-15-mail.md").exists()
    assert "NEED-MARCEL" in captured.err


def test_feedback_command_runs_standalone(env_dirs, capsys):
    exit_code = cli.main(["feedback"])
    assert exit_code == 0
    assert "feedback=0 rules=0" in capsys.readouterr().out
