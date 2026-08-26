from pathlib import Path

from src.mail import extract

FIXTURES = Path(__file__).resolve().parent.parent.parent / "examples" / "mail"


def _fields(name):
    return extract.build_message_fields((FIXTURES / name).read_bytes())


def test_plain_text_extracted_verbatim():
    fields = _fields("001-plain-simple.eml")
    assert "free for lunch this Thursday" in fields["body_text"]
    assert fields["from_addr"] == "priya.nair@example.com"
    assert fields["from_name"] == "Priya Nair"


def test_html_only_converted_to_text_without_scripts_or_images():
    fields = _fields("002-html-only-newsletter.eml")
    body = fields["body_text"]
    assert "Embedded Weekly" in body
    assert "RTOS scheduler benchmark" in body
    assert "<img" not in body
    assert "track.example.com" not in body
    # link text is kept even though the markup around it is stripped
    assert "Unsubscribe" in body


def test_prefers_plain_over_html_in_multipart_alternative():
    fields = _fields("007-multipart-alt-cfp.eml")
    assert "call for papers" in fields["body_text"]
    assert "<html>" not in fields["body_text"]
    assert "<p>" not in fields["body_text"]


def test_german_umlauts_preserved():
    fields = _fields("004-german-plain.eml")
    assert "bestätigen" in fields["body_text"]
    assert "Grüßen" in fields["body_text"]
    assert "Terminbestätigung" in fields["subject"]


def test_rfc2047_subject_and_display_name_decoded():
    fields = _fields("011-rfc2047-subject.eml")
    assert fields["subject"] == "Angebot über Solarpanele – Handlungsbedarf"
    assert fields["from_name"] == "Müller Energie"
    assert fields["from_addr"] == "kontakt@mueller-energie.example"


def test_attachment_metadata_without_content():
    fields = _fields("003-multipart-attachment.eml")
    assert fields["has_attachments"] == 1
    import json

    attachments = json.loads(fields["attachments_json"])
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "mietvertrag.pdf"
    assert attachments[0]["mimetype"] == "application/pdf"
    assert attachments[0]["size"] > 0


def test_no_attachment_case():
    fields = _fields("001-plain-simple.eml")
    assert fields["has_attachments"] == 0
    assert fields["attachments_json"] == "[]"


def test_headers_and_reply_chain():
    fields = _fields("009-digest-reply-feedback.eml")
    assert fields["in_reply_to"] == "<digest-20260814-a1b2c3d4e5f60718@life-agent>"
    assert fields["message_id_hdr"] == "<009-digest-reply@fixtures.example>"


def test_cc_and_reply_to_headers():
    fields = _fields("012-minimal-body.eml")
    assert fields["cc_addrs"] == "team@example.com"
    assert fields["reply_to"] == "jonas.keller+replies@example.com"


def test_injection_fixture_body_is_extracted_as_plain_text_not_executed():
    fields = _fields("008-prompt-injection.eml")
    # extract.py has no model in it at all; it just returns the text as data.
    assert "Ignore all previous instructions" in fields["body_text"]
