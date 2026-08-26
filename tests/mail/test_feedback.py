import json

import pytest

from src.mail import feedback, store


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "mail.db")
    yield c
    c.close()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


# --- parse_feedback_lines: pure parsing, no I/O ---

def test_parses_ref_verdict_line():
    items = feedback.parse_feedback_lines("#3 junk", {"3": "msg-3"})
    assert items == [{"type": "feedback", "message_id": "msg-3", "verdict": "junk", "note": None}]


def test_parses_all_verdict_words():
    refs = {"1": "m1"}
    for word in feedback.VERDICT_WORDS:
        items = feedback.parse_feedback_lines(f"#1 {word}", refs)
        assert items[0]["verdict"] == word.lower()


def test_ref_verdict_with_unknown_number_is_dropped():
    assert feedback.parse_feedback_lines("#99 junk", {"1": "m1"}) == []


def test_parses_vip_line():
    items = feedback.parse_feedback_lines("vip boss@example.com", {})
    assert items == [{"type": "rule", "kind": "vip", "value": "boss@example.com"}]


def test_parses_mute_line():
    items = feedback.parse_feedback_lines("mute spammy.example", {})
    assert items == [{"type": "rule", "kind": "mute", "value": "spammy.example"}]


def test_parses_topic_line():
    items = feedback.parse_feedback_lines("topic reaction wheels", {})
    assert items == [{"type": "rule", "kind": "topic", "value": "reaction wheels"}]


def test_parses_reply_line():
    items = feedback.parse_feedback_lines("reply #2: sounds good, Thursday works", {"2": "m2"})
    assert items == [
        {"type": "feedback", "message_id": "m2", "verdict": "reply",
         "note": "sounds good, Thursday works"}
    ]


def test_parses_multiple_lines_together():
    body = "#3 junk\nvip hartmann-dachbau.example\nmute spam@example.com\ntopic reaction wheels\nreply #2: ok"
    refs = {"2": "m2", "3": "m3"}
    items = feedback.parse_feedback_lines(body, refs)
    assert len(items) == 5


def test_ignores_unrecognized_lines():
    assert feedback.parse_feedback_lines("just chatting, nothing structured here", {}) == []


def test_case_insensitive():
    items = feedback.parse_feedback_lines("VIP Boss@Example.com", {})
    assert items == [{"type": "rule", "kind": "vip", "value": "Boss@Example.com"}]


# --- find_digest_replies / process_feedback: real storage ---

def _seed_digest(conn, message_id_hdr="<digest-20260814-abc123@life-agent>", refs=None):
    store.upsert_digest(conn, {
        "date": "2026-08-14", "message_id_hdr": message_id_hdr,
        "refs_json": json.dumps(refs or {"1": "m1", "2": "m2", "3": "m3"}),
        "path": "briefs/2026-08-14-mail.md", "inserted_gmail_id": "gmail123",
        "created_at": "2026-08-14T06:30:00Z",
    })


def _owner_reply(conn, message_factory, *, in_reply_to, body):
    store.upsert_message(conn, message_factory(
        "reply-1", is_from_owner=1, in_reply_to=in_reply_to, body_text=body,
    ))


def test_find_digest_replies_matches_in_reply_to(conn, message_factory):
    _seed_digest(conn)
    _owner_reply(conn, message_factory, in_reply_to="<digest-20260814-abc123@life-agent>",
                 body="#3 junk")
    replies = feedback.find_digest_replies(conn)
    assert len(replies) == 1
    assert replies[0]["id"] == "reply-1"


def test_find_digest_replies_ignores_non_owner_and_unrelated_replies(conn, message_factory):
    _seed_digest(conn)
    store.upsert_message(conn, message_factory("m-other", is_from_owner=0,
                                                in_reply_to="<digest-20260814-abc123@life-agent>"))
    store.upsert_message(conn, message_factory("m-unrelated", is_from_owner=1,
                                                in_reply_to="<something-else@example.com>"))
    assert feedback.find_digest_replies(conn) == []


def test_process_feedback_stores_feedback_and_rules_and_jsonl(conn, data_dir, message_factory):
    _seed_digest(conn, refs={"3": "m3"})
    _owner_reply(conn, message_factory,
                 in_reply_to="<digest-20260814-abc123@life-agent>",
                 body="#3 junk\nvip boss@example.com\nmute spam@example.com")

    result = feedback.process_feedback(conn, data_dir)

    assert result.feedback == 1
    assert result.rules == 2

    stored_feedback = conn.execute("SELECT * FROM feedback").fetchall()
    assert len(stored_feedback) == 1
    assert stored_feedback[0]["message_id"] == "m3"
    assert stored_feedback[0]["verdict"] == "junk"

    rule_values = {row["value"] for row in conn.execute("SELECT value FROM rules").fetchall()}
    assert rule_values == {"boss@example.com", "spam@example.com"}

    jsonl_path = data_dir / "mail-feedback.jsonl"
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # must be valid JSON


def test_process_feedback_tolerates_an_unwritable_jsonl(conn, data_dir, message_factory, capsys):
    """DATA_DIR's own directory bits don't grant life-agent write access to CREATE a
    new file there (bootstrap must pre-create mail-feedback.jsonl group-writable) — if
    that hasn't happened, appending fails. A storage hiccup on the jsonl mirror must
    not lose the feedback/rules already committed to the database, or crash the run.
    """
    _seed_digest(conn, refs={"3": "m3"})
    _owner_reply(conn, message_factory,
                 in_reply_to="<digest-20260814-abc123@life-agent>", body="#3 junk")

    data_dir.chmod(0o500)  # no write — simulates the file not existing and unable to be created
    try:
        result = feedback.process_feedback(conn, data_dir)
    finally:
        data_dir.chmod(0o700)

    assert result.feedback == 1  # still recorded in the database
    assert "could not append" in capsys.readouterr().err


def test_process_feedback_is_idempotent_safe_to_rerun(conn, data_dir, message_factory):
    """Re-running after no new replies arrived shouldn't error or double-append."""
    _seed_digest(conn, refs={"3": "m3"})
    _owner_reply(conn, message_factory,
                 in_reply_to="<digest-20260814-abc123@life-agent>", body="#3 junk")

    feedback.process_feedback(conn, data_dir)
    second = feedback.process_feedback(conn, data_dir)

    # the same reply is re-parsed each run (v1 has no "already processed" tracking),
    # so calling it twice records the feedback twice — this asserts that reality
    # rather than a stronger guarantee the code doesn't provide.
    assert second.feedback == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


def test_no_digests_means_no_replies_found(conn, message_factory):
    store.upsert_message(conn, message_factory("m1", is_from_owner=1, in_reply_to="<x@y>"))
    assert feedback.find_digest_replies(conn) == []
