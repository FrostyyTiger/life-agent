"""This repo is public. Nothing that identifies the owner may land in it — see the
kickoff rule in life-agent-notes and docs/trust-model.md's repo boundary table.

This test reads the *real* address (and, if set, a real display name) straight from
the private config at $LIFE_AGENT_DATA/config.yaml and asserts neither ever appears
anywhere in this tree. It is intentionally not hardcoded: hardcoding the value here
would itself be the leak.

On a machine without LIFE_AGENT_DATA set (a fresh clone, CI on the public repo), there
is nothing private to check against, so the test skips rather than failing.
"""

import os
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXCLUDED_DIR_NAMES = {".git", ".venv", ".pytest_cache", "__pycache__"}
EXCLUDED_FILES = {"uv.lock"}


def _iter_repo_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        yield path


def _real_identifiers():
    data_dir = os.environ.get("LIFE_AGENT_DATA")
    if not data_dir:
        return None
    config_path = Path(data_dir) / "config.yaml"
    if not config_path.is_file():
        return None
    raw = yaml.safe_load(config_path.read_text()) or {}
    mail_raw = raw.get("mail") or {}
    address = mail_raw.get("address")
    owner_name = mail_raw.get("owner_name")
    identifiers = [str(v) for v in (address, owner_name) if v]
    return identifiers or None


def test_repo_never_contains_the_real_owner_identity():
    identifiers = _real_identifiers()
    if not identifiers:
        pytest.skip("LIFE_AGENT_DATA not set or config.yaml has no mail.address — "
                    "nothing private to check against on this clone")

    hits = []
    for path in _iter_repo_files():
        try:
            text = path.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        for identifier in identifiers:
            if identifier in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}: contains {identifier!r}")

    assert not hits, "real owner identity found in the public repo:\n" + "\n".join(hits)
