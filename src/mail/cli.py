"""Entry point: `python -m src.mail.cli` (wrapped by `bin/mail`)."""

from __future__ import annotations

import argparse
import sys

from src.mail import store
from src.mail.config import ConfigError, Env, MailConfig, load_config, load_env


def _db_path(env: Env):
    return env.state_dir / "mail.db"


def cmd_status(env: Env, config: MailConfig) -> int:
    db_path = _db_path(env)
    existed_before = db_path.exists()
    conn = store.connect(db_path)
    try:
        count = store.count_messages(conn)
    finally:
        conn.close()

    count_str = str(count)
    if not existed_before:
        count_str += " (database just created — run `mail sync`)"

    lines = [
        "mail status",
        f"  LIFE_AGENT_DATA:  {env.data_dir}",
        f"  LIFE_AGENT_STATE: {env.state_dir}",
        f"  LIFE_AGENT_CONF:  {env.conf_dir}",
        f"  mail.address:     {config.address}",
        f"  mail.tag_since:   {config.tag_since.isoformat()}",
        f"  database:         {db_path}",
        f"  messages:         {count_str}",
    ]
    print("\n".join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="print env, DB path, and message counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        env = load_env()
        config = load_config(env.data_dir)
    except ConfigError as exc:
        print(f"mail: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        return cmd_status(env, config)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
