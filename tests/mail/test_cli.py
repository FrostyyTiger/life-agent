from src.mail import cli


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
    assert "messages:         0" in out


def test_status_counts_existing_messages(env_dirs, capsys):
    import sqlite3

    db_path = env_dirs["state"] / "mail.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO messages VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
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
