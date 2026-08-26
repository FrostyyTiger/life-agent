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


def test_search_vec_mode_reports_not_available(env_dirs, capsys):
    exit_code = cli.main(["search", "gutter", "--mode", "vec"])
    assert exit_code == 2
    assert "stage 5" in capsys.readouterr().err


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
