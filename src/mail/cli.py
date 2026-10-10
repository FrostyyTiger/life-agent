"""Entry point: `python -m src.mail.cli` (wrapped by `bin/mail`)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

from src.mail import digest as digest_mod
from src.mail import embed as embed_mod
from src.mail import feedback as feedback_mod
from src.mail import gmail
from src.mail import mail_mcp
from src.mail import search as search_mod
from src.mail import serve as serve_mod
from src.mail import socket_client
from src.mail import store
from src.mail import tag as tag_mod
from src.mail import verify as verify_mod
from src.mail.config import ConfigError, Env, MailConfig, load_config, load_env

# The owner's only path into the archive once bootstrap (stage 8) locks mail.db down
# to the life-agent user — see docs/trust-model.md. A fixed, generic system path, not
# host-specific, so it's fine as a constant in the public repo.
SOCKET_PATH = Path("/run/life-agent/mail.sock")


def _db_path(env: Env):
    return env.state_dir / "mail.db"


def _models_dir(env: Env):
    return env.state_dir / "models"


def _raw_dir(env: Env):
    return env.state_dir / "raw"


def _build_embedder(env: Env):
    return embed_mod.SentenceTransformerEmbedder(_models_dir(env))


def _format_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def _archive_lines(counts: dict) -> list[str]:
    """The archive counts (verify.archive_counts), from a local read or the socket. An
    older query server answers with `messages` only; print what is there.
    """
    if "rows_live" not in counts:
        return []
    if counts.get("gmail_total") is None:
        gmail_total = "unknown (no sync has recorded it yet)"
    else:
        gmail_total = f"{counts['gmail_total']} (at last sync, {counts.get('gmail_total_at')})"
    return [
        f"  gmail total:      {gmail_total}",
        f"  rows:             {counts['rows_live']} live, {counts['rows_deleted']} deleted in "
        "Gmail (kept)",
        f"  raw files:        {counts['raw_files']}",
        f"  pending fetch:    {counts['pending_fetch']}",
        f"  not embedded:     {counts['unembedded']} (searchable by fts only until embedded)",
        f"  disk:             {_format_bytes(counts['disk_bytes'])} (mail.db + raw/)",
    ]


def cmd_status(env: Env, config: MailConfig) -> int:
    db_path = _db_path(env)
    counts: dict = {}
    try:
        existed_before = db_path.exists()
        conn = store.connect(db_path)
        try:
            counts = verify_mod.archive_counts(conn, _raw_dir(env), db_path)
        finally:
            conn.close()
        count_str = str(counts["messages"])
        if not existed_before:
            count_str += " (database just created — run `mail sync`)"
    except sqlite3.OperationalError:
        try:
            counts = socket_client.get(SOCKET_PATH, "/status")
            count_str = f"{counts['messages']} (via socket)"
        except socket_client.SocketQueryError as exc:
            count_str = f"unavailable — {exc}"

    lines = [
        "mail status",
        f"  LIFE_AGENT_DATA:  {env.data_dir}",
        f"  LIFE_AGENT_STATE: {env.state_dir}",
        f"  LIFE_AGENT_CONF:  {env.conf_dir}",
        f"  mail.address:     {config.address}",
        f"  mail.tag_since:   {config.tag_since.isoformat()}",
        f"  database:         {db_path}",
        f"  messages:         {count_str}",
        *_archive_lines(counts),
    ]
    print("\n".join(lines))
    return 0


def _render_hit(row: dict) -> str:
    return f"{row['id']}  {row['date_iso']}  {row['from_addr']:<30}  {row['subject']}"


def cmd_search(env: Env, args: argparse.Namespace) -> int:
    try:
        db_path = _db_path(env)
        conn = store.connect(db_path)
    except sqlite3.OperationalError:
        try:
            payload = socket_client.get(SOCKET_PATH, "/search", {
                "q": args.query, "mode": args.mode, "from": args.from_,
                "since": args.since, "until": args.until, "limit": args.limit,
            })
        except socket_client.SocketQueryError as exc:
            print(f"mail: {exc}", file=sys.stderr)
            return 2
        hits = payload["hits"]
    else:
        embedder = _build_embedder(env) if args.mode in ("vec", "hybrid") else None
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
    try:
        conn = store.connect(_db_path(env))
    except sqlite3.OperationalError:
        try:
            row = socket_client.get(SOCKET_PATH, "/show", {"id": args.id})["message"]
        except socket_client.SocketQueryError as exc:
            print(f"mail: {exc}", file=sys.stderr)
            return 1
    else:
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


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.0f} MB"


def cmd_embed_measure(env: Env, n: int) -> int:
    """`mail embed --measure N`: the numbers decision D1 needs. Commits nothing."""
    rss_before = embed_mod.current_rss_bytes()
    embedder = _build_embedder(env)
    # What the query socket holds once a vec/hybrid search has loaded the model.
    embedder.embed(["measure: one query, as the query service would encode it"])
    rss_loaded = embed_mod.current_rss_bytes()
    vram_loaded = embed_mod.peak_vram_bytes()

    conn = store.connect(_db_path(env))
    try:
        result = embed_mod.measure(conn, embedder, n)
    finally:
        conn.rollback()
        conn.close()
    peak = embed_mod.peak_rss_bytes()
    vram_peak = embed_mod.peak_vram_bytes()

    hours = result["backlog_hours"]
    print("\n".join([
        "embed --measure (nothing committed)",
        f"  messages:              {result['messages']} ({result['chunks']} chunks) "
        f"in {result['seconds']:.1f} s",
        f"  rate:                  {result['messages_per_minute']:.1f} messages/min",
        f"  backlog:               {result['backlog']} un-embedded messages, "
        + (f"~{hours:.1f} h of embed time at this rate" if hours is not None else "rate unknown"),
        f"  RSS before model:      {_mb(rss_before)}",
        f"  model loaded:          RSS {_mb(rss_loaded)}, VRAM "
        + (_mb(vram_loaded) if vram_loaded is not None else "n/a (no CUDA)")
        + " (what mail serve holds after its first vec/hybrid search)",
        f"  peak RSS:              {_mb(peak)} (embed unit MemoryMax=4G)",
        "  peak VRAM:             "
        + (_mb(vram_peak) if vram_peak is not None else "n/a (no CUDA, CPU fallback)"),
    ]))
    return 0


def cmd_embed(env: Env, args: argparse.Namespace) -> int:
    if args.measure is not None:
        return cmd_embed_measure(env, args.measure)
    embedder = _build_embedder(env)
    conn = store.connect(_db_path(env))
    try:
        processed = embed_mod.embed_pending(conn, embedder, budget_seconds=args.budget)
    finally:
        conn.close()
    print(f"embed: processed={processed}")
    return 0


def cmd_tag(env: Env, config: MailConfig, args: argparse.Namespace) -> int:
    conn = store.connect(_db_path(env))
    try:
        result = tag_mod.tag(
            conn, config, conf_dir=env.conf_dir, state_dir=env.state_dir, limit=args.limit
        )
        feedback_result = feedback_mod.process_feedback(conn, env.data_dir)
    finally:
        conn.close()
    print(
        f"tag: tagged={result.tagged} muted={result.muted} failed={result.failed} "
        f"feedback={feedback_result.feedback} rules={feedback_result.rules}"
    )
    return 0


def cmd_feedback(env: Env, args: argparse.Namespace) -> int:
    conn = store.connect(_db_path(env))
    try:
        result = feedback_mod.process_feedback(conn, env.data_dir)
    finally:
        conn.close()
    print(f"feedback: feedback={result.feedback} rules={result.rules}")
    return 0


def _make_insert_fn(env: Env):
    try:
        credentials = gmail.load_credentials(env.conf_dir, gmail.INSERT_TOKEN_FILENAME)
    except gmail.GmailAuthError as exc:
        raise digest_mod.DigestError(str(exc)) from exc
    service = gmail.build_service(credentials)
    return lambda raw_bytes: gmail.insert_raw_message(service, raw_bytes)


def cmd_digest(env: Env, config: MailConfig, args: argparse.Namespace) -> int:
    target_date = date.fromisoformat(args.date) if args.date else None

    insert_fn = None
    insert_setup_error = None
    if not args.dry_run:
        try:
            insert_fn = _make_insert_fn(env)
        except digest_mod.DigestError as exc:
            insert_setup_error = str(exc)

    conn = store.connect(_db_path(env))
    try:
        result = digest_mod.digest(
            conn, config, env.data_dir, conf_dir=env.conf_dir, state_dir=env.state_dir,
            target_date=target_date, dry_run=args.dry_run, insert_fn=insert_fn,
        )
    finally:
        conn.close()

    if result.skipped_existing:
        print(f"digest: {result.path} already exists, not overwriting")
        return 0

    if result.written:
        committed = digest_mod.git_commit_data_repo(
            env.data_dir, f"mail digest: {result.date.isoformat()}"
        )
        if not committed:
            print("digest: nothing to commit in the data repo (or git failed — see above)",
                  file=sys.stderr)

    print(
        f"digest: date={result.date} path={result.path} written={result.written} "
        f"degraded={result.degraded} inserted={result.inserted}"
    )
    if insert_setup_error and not args.dry_run:
        print(f"NEED-MARCEL: {insert_setup_error}", file=sys.stderr)
    if result.insert_error:
        print(f"digest: Gmail insert failed (file still written): {result.insert_error}",
              file=sys.stderr)
    return 0


def _readonly_port(env: Env):
    """The real Gmail port on the readonly token, or None after printing NEED-MARCEL."""
    try:
        credentials = gmail.load_credentials(env.conf_dir, gmail.READONLY_TOKEN_FILENAME)
    except gmail.GmailAuthError as exc:
        print(f"NEED-MARCEL: {exc}", file=sys.stderr)
        return None
    return gmail.RealGmailPort(gmail.build_service(credentials))


def cmd_verify(env: Env) -> int:
    port = _readonly_port(env)
    if port is None:
        return 2
    conn = store.connect(_db_path(env))
    try:
        report = verify_mod.verify(conn, port, _raw_dir(env))
    finally:
        conn.close()
    print(verify_mod.format_report(report))
    return 0 if report.ok else 1


def cmd_backfill_raw(env: Env, args: argparse.Namespace) -> int:
    port = _readonly_port(env)
    if port is None:
        return 2
    conn = store.connect(_db_path(env))
    try:
        result = verify_mod.backfill_raw(conn, port, _raw_dir(env), budget_seconds=args.budget)
    finally:
        conn.close()
    print(f"backfill-raw: written={result.written} unrecoverable={len(result.unrecoverable)} "
          f"remaining={result.remaining}")
    for message_id in result.unrecoverable:
        print(f"backfill-raw: Gmail no longer serves {message_id}; its row stays, with no "
              "raw copy", file=sys.stderr)
    return 0 if not result.unrecoverable and result.remaining == 0 else 1


def cmd_sync(env: Env, config: MailConfig, args: argparse.Namespace) -> int:
    port = _readonly_port(env)
    if port is None:
        return 2

    start = time.monotonic()
    conn = store.connect(_db_path(env))
    try:
        if args.full:
            gmail.reset_sync_state(conn)
        result = gmail.sync(conn, port, config, budget_seconds=args.budget,
                            raw_dir=_raw_dir(env))

        processed = None
        if not args.no_embed:
            embed_budget = None
            if args.budget is not None:
                embed_budget = max(0.0, args.budget - (time.monotonic() - start))
            embedder = _build_embedder(env)
            processed = embed_mod.embed_pending(conn, embedder, budget_seconds=embed_budget)
    finally:
        conn.close()

    embedded = "skipped" if processed is None else processed
    print(
        f"sync: mode={result.mode} fetched={result.fetched} done={result.done} "
        f"embedded={embedded}"
    )
    return 0


def cmd_serve(env: Env, args: argparse.Namespace) -> int:
    socket_path = Path(args.socket) if args.socket else SOCKET_PATH
    print(f"serve: listening on {socket_path}")
    serve_mod.serve(socket_path, _db_path(env), env.state_dir)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    socket_path = Path(args.socket) if args.socket else SOCKET_PATH
    server = mail_mcp.build_server(socket_path)
    server.run(transport="stdio")
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
    sync_parser.add_argument("--full", action="store_true",
                              help="reset sync state and re-run a full backfill/rebuild")
    sync_parser.add_argument("--no-embed", action="store_true",
                              help="fetch only; leave embedding to `mail embed` (its own timer)")

    embed_parser = subparsers.add_parser("embed", help="embed messages that have no chunks yet")
    embed_parser.add_argument("--budget", type=float, default=None, help="seconds")
    embed_parser.add_argument(
        "--measure", type=int, default=None, metavar="N",
        help="embed the N newest messages into a scratch table, print messages/min and "
             "RSS, commit nothing",
    )

    subparsers.add_parser(
        "verify",
        help="prove the archive is complete: Gmail vs. rows vs. raw files (exit 1 on a gap)",
    )

    backfill_raw_parser = subparsers.add_parser(
        "backfill-raw", help="refetch raw messages for rows that have no raw file (repair)"
    )
    backfill_raw_parser.add_argument("--budget", type=float, default=None, help="seconds")

    tag_parser = subparsers.add_parser("tag", help="tag new messages via claude -p")
    tag_parser.add_argument("--limit", type=int, default=None)

    subparsers.add_parser(
        "feedback", help="parse owner replies to digests into feedback/rules"
    )

    digest_parser = subparsers.add_parser("digest", help="compose and send the morning digest")
    digest_parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    digest_parser.add_argument("--dry-run", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="run the read-only query socket")
    serve_parser.add_argument("--socket", default=None, help=f"default: {SOCKET_PATH}")

    mcp_parser = subparsers.add_parser(
        "mcp", help="run a stdio MCP server exposing search/show/status over the socket"
    )
    mcp_parser.add_argument("--socket", default=None, help=f"default: {SOCKET_PATH}")

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
    if args.command == "verify":
        return cmd_verify(env)
    if args.command == "backfill-raw":
        return cmd_backfill_raw(env, args)
    if args.command == "tag":
        return cmd_tag(env, config, args)
    if args.command == "feedback":
        return cmd_feedback(env, args)
    if args.command == "digest":
        return cmd_digest(env, config, args)
    if args.command == "serve":
        return cmd_serve(env, args)
    if args.command == "mcp":
        return cmd_mcp(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
