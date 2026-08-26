"""Composes the morning mail digest and inserts it into the owner's own inbox.

The model (Sonnet) only ever writes prose for two sections — "Needs you" and "Worth
knowing" — one line per item, in an order and count fixed by code. Numbering, the
`#n -> message_id` map (`digests.refs_json`, what `feedback.py` needs to resolve a
reply), the footer, and every other section are assembled deterministically. Degraded
mode never calls the model at all: the two alarms this project cares about (a brief
that must exist, and shouldn't lie about why) don't get to depend on an API call.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.mail import claude_cli, store
from src.mail.config import MailConfig

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
DIGEST_PROMPT_PATH = PROMPTS_DIR / "digest.md"

DIGEST_MODEL = "sonnet"
MAX_WORTH_KNOWING = 15
WHILE_AWAY_THRESHOLD_DAYS = 3
DEFAULT_LOOKBACK_HOURS = 24

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_you_lines": {"type": "array", "items": {"type": "string"}},
        "worth_knowing_lines": {"type": "array", "items": {"type": "string"}},
        "while_away": {"type": ["string", "null"]},
    },
    "required": ["needs_you_lines", "worth_knowing_lines", "while_away"],
    "additionalProperties": False,
}


class DigestError(Exception):
    pass


@dataclass(frozen=True)
class DigestResult:
    date: date
    path: Path
    written: bool
    degraded: bool
    inserted: bool
    insert_error: str | None = None
    skipped_existing: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _since_cutoff(conn, now: datetime) -> str:
    last = store.get_latest_digest(conn)
    if last:
        return last["created_at"]
    return (now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)).isoformat()


def _while_away_days(conn, now: datetime) -> int | None:
    last = store.get_latest_digest(conn)
    if not last:
        return None
    last_dt = datetime.fromisoformat(last["created_at"])
    return (now - last_dt).days


def select_tagged_since(conn, since_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT m.*, t.category AS tag_category, t.importance AS tag_importance, "
        "t.summary AS tag_summary, t.action AS tag_action, t.deadline AS tag_deadline "
        "FROM messages m JOIN tags t ON t.message_id = m.id "
        "WHERE t.tagged_at >= ? AND t.category IS NOT NULL AND m.deleted_at IS NULL "
        "ORDER BY t.importance DESC, m.internal_date ASC",
        (since_iso,),
    ).fetchall()
    return [dict(row) for row in rows]


def select_untagged_since(conn, since_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT m.* FROM messages m "
        "WHERE m.fetched_at >= ? AND m.is_from_owner = 0 AND m.deleted_at IS NULL "
        "ORDER BY m.internal_date ASC",
        (since_iso,),
    ).fetchall()
    return [dict(row) for row in rows]


@dataclass
class Sections:
    needs_you: list[dict] = field(default_factory=list)
    worth_knowing: list[dict] = field(default_factory=list)
    receipts_notifications: list[dict] = field(default_factory=list)
    newsletters: list[dict] = field(default_factory=list)
    junk: list[dict] = field(default_factory=list)


def bucket_messages(tagged: list[dict], config: MailConfig) -> Sections:
    sections = Sections()
    for message in tagged:
        category = message["tag_category"]
        if category == "needs-you" and message["tag_importance"] >= 2:
            sections.needs_you.append(message)
        elif category == "fyi":
            sections.worth_knowing.append(message)
        elif category in ("receipt", "notification"):
            sections.receipts_notifications.append(message)
        elif category == "newsletter":
            sections.newsletters.append(message)
        else:  # junk, unknown
            sections.junk.append(message)

    sections.needs_you = sections.needs_you[: config.max_needs_you]
    sections.worth_knowing = sections.worth_knowing[:MAX_WORTH_KNOWING]
    return sections


def assign_refs(sections: Sections) -> dict[str, str]:
    """#n -> message_id, in display order, for exactly the items shown individually."""
    refs: dict[str, str] = {}
    n = 1
    for message in sections.needs_you + sections.worth_knowing + sections.receipts_notifications:
        refs[str(n)] = message["id"]
        n += 1
    return refs


def _mechanical_line(message: dict) -> str:
    line = f"{message['from_name'] or message['from_addr']} — {message['tag_summary']}"
    if message["tag_action"]:
        line += f" — {message['tag_action']}"
    if message["tag_deadline"]:
        line += f" (by {message['tag_deadline']})"
    return line


def _compose_prose(
    sections: Sections, while_away_days: int | None, *, conf_dir: Path, state_dir: Path,
    claude_runner,
) -> tuple[list[str], list[str], str | None, bool]:
    """Returns (needs_you_lines, worth_knowing_lines, while_away, degraded)."""
    if not sections.needs_you and not sections.worth_knowing:
        return [], [], None, False

    framing = DIGEST_PROMPT_PATH.read_text()
    payload = {
        "needs_you": [
            {"from": m["from_name"] or m["from_addr"], "summary": m["tag_summary"],
             "action": m["tag_action"], "deadline": m["tag_deadline"]}
            for m in sections.needs_you
        ],
        "worth_knowing": [
            {"from": m["from_name"] or m["from_addr"], "summary": m["tag_summary"]}
            for m in sections.worth_knowing
        ],
        "while_away_days": while_away_days,
    }
    prompt = framing + "\n\n" + json.dumps(payload)

    try:
        response = claude_runner(
            prompt, model=DIGEST_MODEL, json_schema=DIGEST_SCHEMA,
            conf_dir=conf_dir, state_dir=state_dir,
        )
        needs_you_lines = response["needs_you_lines"]
        worth_knowing_lines = response["worth_knowing_lines"]
        if (
            len(needs_you_lines) != len(sections.needs_you)
            or len(worth_knowing_lines) != len(sections.worth_knowing)
        ):
            raise DigestError("model returned the wrong number of lines")
        return needs_you_lines, worth_knowing_lines, response.get("while_away"), False
    except (claude_cli.ClaudeCliError, DigestError, KeyError, TypeError):
        return (
            [_mechanical_line(m) for m in sections.needs_you],
            [_mechanical_line(m) for m in sections.worth_knowing],
            None,
            True,
        )


def _render_markdown(
    target_date: date, sections: Sections, needs_you_lines: list[str],
    worth_knowing_lines: list[str], while_away: str | None, degraded: bool,
    pending_replies: int, subjects_only: list[dict] | None = None,
) -> str:
    lines = [f"# Digest — {target_date.strftime('%A %-d %b')}", ""]
    if degraded:
        lines.append("_Degraded: composed without a model this run._")
        lines.append("")
    if while_away:
        lines.append(while_away)
        lines.append("")

    if subjects_only is not None:
        lines.append("## New mail")
        if subjects_only:
            for i, message in enumerate(subjects_only, start=1):
                lines.append(f"{i}. {message['from_name'] or message['from_addr']} — {message['subject']}")
        else:
            lines.append("(none)")
        lines.append("")
    else:
        n = 1
        lines.append("## Needs you")
        for line in needs_you_lines:
            lines.append(f"{n}. {line}")
            n += 1
        if not needs_you_lines:
            lines.append("(none)")
        lines.append("")

        lines.append("## Worth knowing")
        for line in worth_knowing_lines:
            lines.append(f"{n}. {line}")
            n += 1
        if not worth_knowing_lines:
            lines.append("(none)")
        lines.append("")

        lines.append("## Receipts & notifications")
        if sections.receipts_notifications:
            lines.append(f"{len(sections.receipts_notifications)} total:")
            for message in sections.receipts_notifications:
                lines.append(f"{n}. {message['from_name'] or message['from_addr']} — {message['subject']}")
                n += 1
        else:
            lines.append("(none)")
        lines.append("")

        lines.append("## Newsletters")
        lines.append(f"{len(sections.newsletters)} total" if sections.newsletters else "(none)")
        lines.append("")

        lines.append("## Junk")
        lines.append(f"{len(sections.junk)} total" if sections.junk else "(none)")
        lines.append("")

    if pending_replies:
        lines.append(
            f"_Noted {pending_replies} reply request(s) from your last message — "
            f"drafting replies isn't implemented yet (v2)._"
        )
        lines.append("")

    lines.append(
        "---\nReply commands: `#n junk|important|fyi|needs-you|receipt` · "
        "`vip <addr|domain>` · `mute <addr|domain>` · `topic <words>` · `reply #n: <text>`"
    )
    return "\n".join(lines)


def _build_email(config: MailConfig, target_date: date, markdown: str) -> tuple[bytes, str]:
    from email.message import EmailMessage
    from email.utils import format_datetime

    message_id = f"<digest-{target_date.strftime('%Y%m%d')}-{secrets.token_hex(8)}@life-agent>"
    subject = f"Digest — {target_date.strftime('%A %-d %b')}"
    html = "<html><body><pre>" + markdown.replace("&", "&amp;").replace("<", "&lt;") + "</pre></body></html>"

    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Subject"] = subject
    msg["From"] = f'"life-agent" <{config.address}>'
    msg["To"] = config.address  # constant, code-controlled — never model output
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(markdown)
    msg.add_alternative(html, subtype="html")

    return bytes(msg), message_id


def compose(
    conn, config: MailConfig, target_date: date, *, conf_dir: Path, state_dir: Path,
    claude_runner=claude_cli.run,
) -> tuple[str, str, bool]:
    """Returns (markdown, refs_json, degraded)."""
    now = datetime.now(timezone.utc)
    since_iso = _since_cutoff(conn, now)
    while_away_days = _while_away_days(conn, now)
    while_away_days_for_prompt = (
        while_away_days if (while_away_days and while_away_days > WHILE_AWAY_THRESHOLD_DAYS) else None
    )

    pending_replies = store.get_unacknowledged_reply_feedback(conn)

    tagged = select_tagged_since(conn, since_iso)
    if not tagged:
        untagged = select_untagged_since(conn, since_iso)
        markdown = _render_markdown(
            target_date, Sections(), [], [], None, degraded=False,
            pending_replies=len(pending_replies), subjects_only=untagged,
        )
        if pending_replies:
            store.acknowledge_feedback(conn, [f["id"] for f in pending_replies], _now_iso())
        return markdown, json.dumps({}), False

    sections = bucket_messages(tagged, config)
    refs = assign_refs(sections)
    needs_you_lines, worth_knowing_lines, while_away, degraded = _compose_prose(
        sections, while_away_days_for_prompt, conf_dir=conf_dir, state_dir=state_dir,
        claude_runner=claude_runner,
    )
    markdown = _render_markdown(
        target_date, sections, needs_you_lines, worth_knowing_lines, while_away,
        degraded, pending_replies=len(pending_replies),
    )

    if pending_replies:
        store.acknowledge_feedback(conn, [f["id"] for f in pending_replies], _now_iso())

    return markdown, json.dumps(refs), degraded


def digest(
    conn, config: MailConfig, data_dir: Path, *, conf_dir: Path, state_dir: Path,
    target_date: date | None = None, dry_run: bool = False,
    claude_runner=claude_cli.run, insert_fn=None,
) -> DigestResult:
    if target_date is None:
        from zoneinfo import ZoneInfo

        target_date = datetime.now(ZoneInfo(config.timezone)).date()

    briefs_dir = data_dir / "briefs"
    path = briefs_dir / f"{target_date.isoformat()}-mail.md"

    if path.exists():
        return DigestResult(
            date=target_date, path=path, written=False, degraded=False,
            inserted=False, skipped_existing=True,
        )

    markdown, refs_json, degraded = compose(
        conn, config, target_date, conf_dir=conf_dir, state_dir=state_dir,
        claude_runner=claude_runner,
    )

    if dry_run:
        return DigestResult(date=target_date, path=path, written=False, degraded=degraded,
                             inserted=False)

    briefs_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)

    inserted = False
    insert_error = None
    message_id_hdr = None
    inserted_gmail_id = None

    if insert_fn is not None:
        try:
            mime_bytes, message_id_hdr = _build_email(config, target_date, markdown)
            inserted_gmail_id = insert_fn(mime_bytes)
            inserted = True
        except Exception as exc:  # noqa: BLE001 - any insert failure is logged, not fatal
            insert_error = str(exc)

    store.upsert_digest(conn, {
        "date": target_date.isoformat(),
        "message_id_hdr": message_id_hdr,
        "refs_json": refs_json,
        "path": str(path),
        "inserted_gmail_id": inserted_gmail_id,
        "created_at": _now_iso(),
    })

    return DigestResult(
        date=target_date, path=path, written=True, degraded=degraded,
        inserted=inserted, insert_error=insert_error,
    )


def git_commit_data_repo(data_dir: Path, message: str) -> bool:
    """Commits everything changed in the data repo. Returns False (does not raise) if
    there was nothing to commit or git isn't available — this is a convenience for the
    agent's own audit trail, not something that should fail the digest run.
    """
    try:
        subprocess.run(["git", "-C", str(data_dir), "add", "-A"], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", str(data_dir), "commit", "-m", message],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
