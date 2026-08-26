"""Parses the owner's replies to digest mails into feedback + rules.

Runs at the end of every `mail tag`. Reply lines the owner can write:

    #3 junk|important|fyi|needs-you|receipt   verdict on tag #3 in the referenced digest
    vip <addr|domain>                         floors importance to >=2 for that sender
    mute <addr|domain>                        tags as junk without a model call
    topic <words>                             a free-text hint folded into future tagging
    reply #3: <text>                          stored only — v1 does not draft replies

Everything lands in the `feedback`/`rules` tables and is mirrored, append-only, to
`$LIFE_AGENT_DATA/mail-feedback.jsonl` so the record survives outside the (unversioned)
mail.db too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.mail import store

VERDICT_WORDS = ("junk", "important", "fyi", "needs-you", "receipt")

_REF_VERDICT_RE = re.compile(
    r"^#(\d+)\s+(" + "|".join(re.escape(w) for w in VERDICT_WORDS) + r")\s*$", re.IGNORECASE
)
_REPLY_RE = re.compile(r"^reply\s+#(\d+):\s*(.+)$", re.IGNORECASE)
_VIP_RE = re.compile(r"^vip\s+(\S+)\s*$", re.IGNORECASE)
_MUTE_RE = re.compile(r"^mute\s+(\S+)\s*$", re.IGNORECASE)
_TOPIC_RE = re.compile(r"^topic\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class FeedbackResult:
    feedback: int
    rules: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_digest_replies(conn) -> list[dict]:
    """Mail from the owner whose In-Reply-To/References names a digest Message-ID
    this archive actually sent."""
    digest_msg_ids = {
        row["message_id_hdr"]
        for row in conn.execute(
            "SELECT message_id_hdr FROM digests WHERE message_id_hdr IS NOT NULL"
        ).fetchall()
    }
    if not digest_msg_ids:
        return []

    candidates = conn.execute(
        "SELECT * FROM messages WHERE is_from_owner = 1 AND deleted_at IS NULL "
        "AND (in_reply_to IS NOT NULL OR references_hdr IS NOT NULL)"
    ).fetchall()

    replies = []
    for row in candidates:
        message = dict(row)
        refs = {message["in_reply_to"]} | set((message["references_hdr"] or "").split())
        if refs & digest_msg_ids:
            replies.append(message)
    return replies


def _refs_map_for_reply(conn, message: dict) -> dict[str, str]:
    """The `n -> message_id` map of whichever digest this reply responds to."""
    refs = [r for r in ({message["in_reply_to"]} | set((message["references_hdr"] or "").split())) if r]
    if not refs:
        return {}
    placeholders = ", ".join("?" for _ in refs)
    row = conn.execute(
        f"SELECT refs_json FROM digests WHERE message_id_hdr IN ({placeholders})", refs
    ).fetchone()
    if not row or not row["refs_json"]:
        return {}
    return json.loads(row["refs_json"])


def parse_feedback_lines(body: str, refs: dict[str, str]) -> list[dict]:
    """Pure parsing: body text + the digest's `n -> message_id` map -> a list of
    `{"type": "feedback", ...}` / `{"type": "rule", ...}` dicts. No I/O, easy to test
    in isolation from storage.
    """
    items: list[dict] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _REF_VERDICT_RE.match(line)
        if m:
            n, verdict = m.group(1), m.group(2).lower()
            message_id = refs.get(n)
            if message_id:
                items.append(
                    {"type": "feedback", "message_id": message_id, "verdict": verdict, "note": None}
                )
            continue

        m = _REPLY_RE.match(line)
        if m:
            n, text = m.group(1), m.group(2).strip()
            message_id = refs.get(n)
            if message_id:
                items.append(
                    {"type": "feedback", "message_id": message_id, "verdict": "reply", "note": text}
                )
            continue

        m = _VIP_RE.match(line)
        if m:
            items.append({"type": "rule", "kind": "vip", "value": m.group(1)})
            continue

        m = _MUTE_RE.match(line)
        if m:
            items.append({"type": "rule", "kind": "mute", "value": m.group(1)})
            continue

        m = _TOPIC_RE.match(line)
        if m:
            items.append({"type": "rule", "kind": "topic", "value": m.group(1).strip()})
            continue

    return items


def _append_jsonl(data_dir: Path, record: dict) -> None:
    path = data_dir / "mail-feedback.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def process_feedback(conn, data_dir: Path) -> FeedbackResult:
    replies = find_digest_replies(conn)

    feedback_count = 0
    rule_count = 0

    for message in replies:
        refs = _refs_map_for_reply(conn, message)
        for item in parse_feedback_lines(message.get("body_text") or "", refs):
            created_at = _now_iso()
            if item["type"] == "feedback":
                store.add_feedback(
                    conn, message_id=item["message_id"], verdict=item["verdict"],
                    note=item["note"], source_msg_id=message["id"], created_at=created_at,
                )
                _append_jsonl(data_dir, {**item, "source_msg_id": message["id"],
                                          "created_at": created_at})
                feedback_count += 1
            else:
                store.add_rule(
                    conn, kind=item["kind"], value=item["value"], created_at=created_at,
                    source="feedback",
                )
                _append_jsonl(data_dir, {**item, "source_msg_id": message["id"],
                                          "created_at": created_at})
                rule_count += 1

    return FeedbackResult(feedback=feedback_count, rules=rule_count)
