"""Entry point: `python -m src.mail.cli` (wrapped by `bin/mail`)."""

from __future__ import annotations

import argparse
import json
import sys
import time

from src.mail import embed as embed_mod
from src.mail import gmail
from src.mail import search as search_mod
from src.mail import store
from src.mail.config import ConfigError, Env, MailConfig, load_config, load_env


def _db_path(env: Env):
    return env.state_dir / "mail.db"


def _models_dir(env: Env):
    return env.state_dir / "models"


def _build_embedder(env: Env):
    return embed_mod.SentenceTransformerEmbedder(_models_dir(env))


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


def _render_hit(row: dict) -> str:
    return f"{row['id']}  {row['date_iso']}  {row['from_addr']:<30}  {row['subject']}"


def cmd_search(env: Env, args: argparse.Namespace) -> int:
    embedder = _build_embedder(env) if args.mode in ("vec", "hybrid") else None

    conn = store.connect(_db_path(env))
    try:
        try:
            hits = search_mod.search(
                conn,
                args.query,
                mode=args.mode,
                embedder=embedder,
                from_filter=args.from_,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
        except search_mod.SearchError as exc:
            print(f"mail: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    if args.json:
        print(json.dumps(hits, indent=2))
    elif not hits:
        print("no results")
    else:
        for row in hits:
            print(_render_hit(row))
    return 0


def cmd_show(env: Env, args: argparse.Namespace) -> int:
    conn = store.connect(_db_path(env))
    try:
        row = store.get_message(conn, args.id)
    finally:
        conn.close()

    if row is None:
        print(f"mail: no message with id {args.id!r}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(row, indent=2))
        return 0

    print(f"Id:        {row['id']}")
    print(f"Date:      {row['date_iso']}")
    print(f"From:      {row['from_name']} <{row['from_addr']}>")
    print(f"To:        {row['to_addrs']}")
    if row["cc_addrs"]:
        print(f"Cc:        {row['cc_addrs']}")
    print(f"Subject:   {row['subject']}")
    if row["has_attachments"]:
        print(f"Attachments: {row['attachments_json']}")
    print()
    print(row["body_text"])
    return 0


def cmd_auth(env: Env, args: argparse.Namespace) -> int:
    try:
        if args.scope == "readonly":
            gmail.auth_readonly(env.conf_dir)
        else:
            gmail.auth_insert(env.conf_dir)
    except gmail.GmailAuthError as exc:
        print(f"NEED-MARCEL: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_embed(env: Env, args: argparse.Namespace) -> int:
    embedder = _build_embedder(env)
    conn = store.connect(_db_path(env))
    try:
        processed = embed_mod.embed_pending(conn, embedder, budget_seconds=args.budget)
    finally:
        conn.close()
    print(f"embed: processed={processed}")
    return 0


def cmd_sync(env: Env, config: MailConfig, args: argparse.Namespace) -> int:
    try:
        credentials = gmail.load_credentials(env.conf_dir, gmail.READONLY_TOKEN_FILENAME)
    except gmail.GmailAuthError as exc:
        print(f"NEED-MARCEL: {exc}", file=sys.stderr)
        return 2

    service = gmail.build_service(credentials)
    port = gmail.RealGmailPort(service)

    start = time.monotonic()
    conn = store.connect(_db_path(env))
    try:
        result = gmail.sync(conn, port, config, budget_seconds=args.budget)

        embed_budget = None
        if args.budget is not None:
            embed_budget = max(0.0, args.budget - (time.monotonic() - start))
        embedder = _build_embedder(env)
        processed = embed_mod.embed_pending(conn, embedder, budget_seconds=embed_budget)
    finally:
        conn.close()

    print(
        f"sync: mode={result.mode} fetched={result.fetched} done={result.done} "
        f"embedded={processed}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="print env, DB path, and message counts")

    search_parser = subparsers.add_parser("search", help="search the archive")
    search_parser.add_argument("query")
    search_parser.add_argument("--mode", default="fts", choices=["fts", "vec", "hybrid"])
    search_parser.add_argument("--from", dest="from_", default=None)
    search_parser.add_argument("--since", default=None, help="YYYY-MM or YYYY-MM-DD")
    search_parser.add_argument("--until", default=None, help="YYYY-MM or YYYY-MM-DD")
    search_parser.add_argument("--limit", type=int, default=search_mod.DEFAULT_LIMIT)
    search_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show", help="show one message")
    show_parser.add_argument("id")
    show_parser.add_argument("--json", action="store_true")

    auth_parser = subparsers.add_parser("auth", help="run the Gmail OAuth flow")
    auth_parser.add_argument("scope", choices=["readonly", "insert"])

    sync_parser = subparsers.add_parser("sync", help="fetch new/changed mail from Gmail")
    sync_parser.add_argument("--budget", type=float, default=None, help="seconds")

    embed_parser = subparsers.add_parser("embed", help="embed messages that have no chunks yet")
    embed_parser.add_argument("--budget", type=float, default=None, help="seconds")

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
    if args.command == "search":
        return cmd_search(env, args)
    if args.command == "show":
        return cmd_show(env, args)
    if args.command == "auth":
        return cmd_auth(env, args)
    if args.command == "sync":
        return cmd_sync(env, config, args)
    if args.command == "embed":
        return cmd_embed(env, args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
