"""Mail tagging via `claude -p` (haiku, no tools — see claude_cli.py).

Rules are applied before the model ever sees a message: a muted sender is tagged
`junk` with zero model calls; a VIP sender's tag is floored to importance >= 2 after
the model has already produced its own judgement. Everything else goes to the model in
batches, each mail wrapped in an explicit `<mail id="…">` block the prompt tells the
model is untrusted data, not instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from src.mail import claude_cli, store
from src.mail.config import MailConfig

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
TAG_PROMPT_PATH = PROMPTS_DIR / "tag.md"

TAG_MODEL = "haiku"
BATCH_SIZE = 10
MAX_ATTEMPTS = 3
MAX_FEEDBACK_EXAMPLES = 20
BODY_TRUNCATION_SUFFIX = "\n[truncated]"

CATEGORIES = ("needs-you", "fyi", "receipt", "notification", "newsletter", "junk")

TAG_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "importance": {"type": "integer", "minimum": 0, "maximum": 3},
        "summary": {"type": "string", "maxLength": 200},
        "action": {"type": ["string", "null"], "maxLength": 120},
        "deadline": {"type": ["string", "null"]},
        "people": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "category", "importance", "summary", "action", "deadline", "people"],
    "additionalProperties": False,
}

TAG_BATCH_SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": TAG_ITEM_SCHEMA}},
    "required": ["tags"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TagResult:
    tagged: int
    muted: int
    failed: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def is_muted(conn, config: MailConfig, message: dict) -> bool:
    """Static config.mute_senders plus anything the owner has since muted via a digest
    reply (`mute <addr|domain>`, stored in `rules`). A rule value is matched as an
    address if it contains "@", else as a domain.
    """
    from_addr = (message["from_addr"] or "").lower()
    domain = _domain(from_addr)
    static = {s.lower() for s in config.mute_senders}
    rule_values = store.get_rule_values(conn, "mute")
    rule_addrs = {v for v in rule_values if "@" in v}
    rule_domains = {v for v in rule_values if "@" not in v}
    return from_addr in static or from_addr in rule_addrs or domain in rule_domains


def is_vip(conn, config: MailConfig, message: dict) -> bool:
    """Static config.vip_senders/vip_domains plus feedback-learned `vip` rules (same
    address-or-domain matching as is_muted)."""
    from_addr = (message["from_addr"] or "").lower()
    domain = _domain(from_addr)
    static_addrs = {s.lower() for s in config.vip_senders}
    static_domains = {d.lower() for d in config.vip_domains}
    rule_values = store.get_rule_values(conn, "vip")
    rule_addrs = {v for v in rule_values if "@" in v}
    rule_domains = {v for v in rule_values if "@" not in v}
    return (
        from_addr in static_addrs or from_addr in rule_addrs
        or domain in static_domains or domain in rule_domains
    )


def select_messages_to_tag(conn, config: MailConfig, limit: int | None = None) -> list[dict]:
    """Messages worth a tag: on/after tag_since, not from the owner, not deleted, not
    one of our own digest mails, and not already successfully tagged or given up on.
    """
    cutoff_ms = int(
        datetime.combine(config.tag_since, time.min, tzinfo=timezone.utc).timestamp() * 1000
    )
    sql = (
        "SELECT m.* FROM messages m LEFT JOIN tags t ON t.message_id = m.id "
        "WHERE m.deleted_at IS NULL AND m.is_from_owner = 0 AND m.internal_date >= ? "
        "AND (m.message_id_hdr IS NULL OR m.message_id_hdr NOT LIKE '<digest-%') "
        "AND (t.message_id IS NULL OR (t.category IS NULL AND t.attempts < ?)) "
        "ORDER BY m.internal_date ASC"
    )
    params: list = [cutoff_ms, MAX_ATTEMPTS]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# A mail can contain a literal "<mail id=...>" / "</mail>" (or our own truncation
# marker) in its subject/from/body, all of which are untrusted, attacker-controlled
# text. Left alone, that would let a message fake the end of its own block and open a
# forged one — e.g. impersonating a different id with fabricated instructions right
# next to the real framing. Neutralize the exact substrings the model would otherwise
# read as block boundaries, in whatever untrusted field they appear.
_MAIL_TAG_RE = re.compile(r"</?mail\b", re.IGNORECASE)
_TRUNCATION_MARKER_RE = re.compile(re.escape("[truncated]"), re.IGNORECASE)


def _sanitize_untrusted(text: str) -> str:
    text = _MAIL_TAG_RE.sub(lambda m: m.group(0).replace("<", "＜"), text)
    text = _TRUNCATION_MARKER_RE.sub("［truncated］", text)
    return text


def _render_mail_block(message: dict, body_chars_for_model: int) -> str:
    subject = _sanitize_untrusted(message["subject"] or "")
    from_name = _sanitize_untrusted(message["from_name"] or "")
    from_addr = _sanitize_untrusted(message["from_addr"] or "")
    body = _sanitize_untrusted(message.get("body_text") or "")

    truncated = len(body) > body_chars_for_model
    if truncated:
        body = body[:body_chars_for_model] + BODY_TRUNCATION_SUFFIX

    return (
        f'<mail id="{message["id"]}">\n'
        f"Subject: {subject}\n"
        f"From: {from_name} <{from_addr}>\n"
        f"Date: {message['date_iso']}\n"
        f"{body}\n"
        f"</mail>"
    )


def build_prompt(config: MailConfig, messages: list[dict], feedback_examples: list[dict]) -> str:
    framing = TAG_PROMPT_PATH.read_text()

    hints = ["Owner hints:"]
    hints.append(f"- vip senders: {', '.join(config.vip_senders) or '(none)'}")
    hints.append(f"- vip domains: {', '.join(config.vip_domains) or '(none)'}")
    hints.append(f"- muted senders: {', '.join(config.mute_senders) or '(none)'}")
    hints.append(f"- topics of interest: {', '.join(config.topics) or '(none)'}")

    feedback_lines = ["Recent feedback from the owner (verdict on a past tag):"]
    if feedback_examples:
        for item in feedback_examples:
            feedback_lines.append(f"- {item['verdict']}: {item.get('note') or ''}".rstrip())
    else:
        feedback_lines.append("(none yet)")

    mail_blocks = [
        _render_mail_block(message, config.body_chars_for_model) for message in messages
    ]

    return "\n\n".join(
        [framing, "\n".join(hints), "\n".join(feedback_lines), "\n\n".join(mail_blocks)]
    )


def _validate_tag_item(item, expected_ids: set[str]) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get("id") not in expected_ids:
        return None
    if item.get("category") not in CATEGORIES:
        return None
    importance = item.get("importance")
    if not isinstance(importance, int) or not (0 <= importance <= 3):
        return None
    summary = item.get("summary")
    if not isinstance(summary, str) or len(summary) > 200:
        return None
    action = item.get("action")
    if action is not None and (not isinstance(action, str) or len(action) > 120):
        return None
    deadline = item.get("deadline")
    if deadline is not None:
        try:
            date.fromisoformat(deadline)
        except (TypeError, ValueError):
            return None
    people = item.get("people")
    if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
        return None
    return item


def _validate_response(response: dict, expected_ids: set[str]) -> dict[str, dict]:
    tags = response.get("tags")
    if not isinstance(tags, list):
        raise claude_cli.ClaudeCliError("response had no `tags` array")

    valid: dict[str, dict] = {}
    for item in tags:
        validated = _validate_tag_item(item, expected_ids)
        if validated is not None:
            valid[validated["id"]] = validated
    return valid


def _record_failed_attempt(conn, message_id: str, error: str) -> None:
    existing = store.get_tag(conn, message_id)
    attempts = (existing["attempts"] if existing else 0) + 1
    category = "unknown" if attempts >= MAX_ATTEMPTS else None
    store.upsert_tag(
        conn,
        {
            "message_id": message_id,
            "category": category,
            "importance": None,
            "summary": None,
            "action": None,
            "deadline": None,
            "people_json": None,
            "model": None,
            "attempts": attempts,
            "tagged_at": _now_iso(),
            "error": error,
        },
    )


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def tag(
    conn,
    config: MailConfig,
    *,
    conf_dir: Path,
    state_dir: Path,
    limit: int | None = None,
    claude_runner=claude_cli.run,
) -> TagResult:
    messages = select_messages_to_tag(conn, config, limit)

    to_model = []
    muted = 0
    for message in messages:
        if is_muted(conn, config, message):
            store.upsert_tag(
                conn,
                {
                    "message_id": message["id"],
                    "category": "junk",
                    "importance": 0,
                    "summary": "muted sender",
                    "action": None,
                    "deadline": None,
                    "people_json": "[]",
                    "model": "rule:mute",
                    "attempts": 1,
                    "tagged_at": _now_iso(),
                    "error": None,
                },
            )
            muted += 1
        else:
            to_model.append(message)

    tagged = 0
    failed = 0

    for batch in _chunks(to_model, BATCH_SIZE):
        feedback_examples = store.get_recent_feedback(conn, MAX_FEEDBACK_EXAMPLES)
        prompt = build_prompt(config, batch, feedback_examples)
        expected_ids = {m["id"] for m in batch}

        try:
            response = claude_runner(
                prompt,
                model=TAG_MODEL,
                json_schema=TAG_BATCH_SCHEMA,
                conf_dir=conf_dir,
                state_dir=state_dir,
            )
            results_by_id = _validate_response(response, expected_ids)
        except claude_cli.ClaudeCliError as exc:
            for message in batch:
                _record_failed_attempt(conn, message["id"], str(exc))
                failed += 1
            continue

        for message in batch:
            result = results_by_id.get(message["id"])
            if result is None:
                _record_failed_attempt(
                    conn, message["id"], "model omitted or invalidly tagged this id"
                )
                failed += 1
                continue

            importance = result["importance"]
            if is_vip(conn, config, message):
                importance = max(importance, 2)

            store.upsert_tag(
                conn,
                {
                    "message_id": message["id"],
                    "category": result["category"],
                    "importance": importance,
                    "summary": result["summary"],
                    "action": result["action"],
                    "deadline": result["deadline"],
                    "people_json": json.dumps(result["people"]),
                    "model": TAG_MODEL,
                    "attempts": 1,
                    "tagged_at": _now_iso(),
                    "error": None,
                },
            )
            tagged += 1

    return TagResult(tagged=tagged, muted=muted, failed=failed)
