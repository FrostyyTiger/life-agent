"""Turn a raw RFC 822 message into the header/body fields `store.py`'s schema wants.

Deliberately knows nothing about Gmail — it operates on raw bytes, so both the real
sync path (stage 3) and tests (against `examples/mail/*.eml`) go through the same code.
`email.policy.default` does the RFC 2047 header decoding for us; the only extraction
logic actually written here is preferring text/plain over HTML, and turning HTML into
readable text when that's all a message has.
"""

from __future__ import annotations

import email
import email.policy
import json
from email.message import EmailMessage

from bs4 import BeautifulSoup


def parse_message(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _html_to_text(html: str) -> str:
    """HTML -> text: drop scripts/styles/images (tracking pixels included), keep link text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "img"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def extract_body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content().strip()

    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        return _html_to_text(html_part.get_content()).strip()

    return ""


def extract_attachments(msg: EmailMessage) -> list[dict]:
    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            {
                "filename": part.get_filename() or "unnamed",
                "mimetype": part.get_content_type(),
                "size": len(payload),
            }
        )
    return attachments


def _header_str(msg: EmailMessage, name: str) -> str | None:
    value = msg.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _address_list(msg: EmailMessage, name: str) -> str:
    header = msg.get(name)
    if header is None:
        return ""
    try:
        return ", ".join(addr.addr_spec for addr in header.addresses)
    except AttributeError:
        return str(header)


def extract_from(msg: EmailMessage) -> tuple[str, str]:
    """Returns (display_name, address); either half may be empty."""
    header = msg.get("From")
    if header is None:
        return "", ""
    try:
        addr = header.addresses[0]
        return addr.display_name or "", addr.addr_spec
    except (AttributeError, IndexError):
        return "", str(header)


def build_message_fields(raw: bytes) -> dict:
    """Fields derivable from the raw message alone.

    The caller (gmail.py in stage 3) merges these with the Gmail-specific fields —
    id, thread_id, history_id, internal_date, date_iso, labels_json, size, fetched_at,
    is_from_owner — that don't come from the RFC 822 content itself.
    """
    msg = parse_message(raw)
    from_name, from_addr = extract_from(msg)
    attachments = extract_attachments(msg)

    return {
        "from_addr": from_addr,
        "from_name": from_name,
        "to_addrs": _address_list(msg, "To"),
        "cc_addrs": _address_list(msg, "Cc"),
        "reply_to": _address_list(msg, "Reply-To"),
        "message_id_hdr": _header_str(msg, "Message-ID"),
        "in_reply_to": _header_str(msg, "In-Reply-To"),
        "references_hdr": _header_str(msg, "References"),
        "subject": str(msg.get("Subject") or ""),
        "body_text": extract_body_text(msg),
        "has_attachments": 1 if attachments else 0,
        "attachments_json": json.dumps(attachments),
    }
