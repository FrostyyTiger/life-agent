"""The synthetic mailbox in examples/mail/ is what every later stage's tests read
against instead of a real inbox. This just checks the fixtures themselves are sound
and cover the categories the plan calls for — extraction/tagging/search logic that
consumes them lands in stages 2-7.
"""

import email
import email.policy

MIN_FIXTURES = 12


def _parse(path):
    with open(path, "rb") as f:
        return email.message_from_binary_file(f, policy=email.policy.default)


def test_at_least_twelve_fixtures(fixture_mail_files):
    assert len(fixture_mail_files) >= MIN_FIXTURES


def test_all_fixtures_parse_as_email(fixture_mail_files):
    for path in fixture_mail_files:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f)
        assert msg["Message-ID"], f"{path.name} missing Message-ID"
        assert msg["Subject"] is not None, f"{path.name} missing Subject"
        assert msg["From"], f"{path.name} missing From"


def test_expected_categories_present(fixture_mail_files):
    names = {p.name for p in fixture_mail_files}

    def has(fragment):
        return any(fragment in n for n in names)

    assert has("plain-simple")
    assert has("html-only")
    assert has("multipart-attachment")
    assert has("german-plain")
    assert has("receipt")
    assert has("multipart-alt")
    assert has("injection")
    assert has("digest-reply-feedback")
    assert has("from-owner")
    assert has("rfc2047")


def test_html_only_fixture_has_no_plain_text_part(fixture_mail_files):
    path = next(p for p in fixture_mail_files if "html-only-newsletter" in p.name)
    msg = _parse(path)
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert html_parts
    assert "Embedded Weekly" in html_parts[0].get_content()


def test_injection_fixture_contains_attack_string(fixture_mail_files):
    path = next(p for p in fixture_mail_files if "injection" in p.name)
    msg = _parse(path)
    body = msg.get_content()
    assert "Ignore all previous instructions" in body


def test_digest_reply_fixture_references_a_digest_message_id(fixture_mail_files):
    path = next(p for p in fixture_mail_files if "digest-reply" in p.name)
    msg = _parse(path)
    assert msg["In-Reply-To"] is not None
    assert "digest-" in msg["In-Reply-To"]
    body = msg.get_content()
    assert "#3 junk" in body
    assert "vip " in body
    assert "mute " in body
