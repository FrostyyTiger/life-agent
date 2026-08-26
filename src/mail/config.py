"""Environment and configuration loading for the mail subsystem.

Three directories gate everything mail-related and none of them has a default —
see docs/plans/mail-v1.md's state-location table. A fallback path here is how a
test run ends up touching a real mailbox or a real archive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

ENV_VARS = ("LIFE_AGENT_DATA", "LIFE_AGENT_STATE", "LIFE_AGENT_CONF")

DEFAULT_MAX_NEEDS_YOU = 5
DEFAULT_BODY_CHARS_FOR_MODEL = 4000
DEFAULT_DIGEST_TIME = "06:30"
DEFAULT_TIMEZONE = "Europe/Zurich"


class ConfigError(Exception):
    """Raised for anything that should stop a mail command before it does work."""


@dataclass(frozen=True)
class Env:
    data_dir: Path
    state_dir: Path
    conf_dir: Path


def load_env() -> Env:
    """Read the three required directories from the environment.

    Refuses to start if any is unset — there is no default and no fallback.
    """
    missing = [name for name in ENV_VARS if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + " (no defaults exist for these on purpose; see docs/plans/mail-v1.md)"
        )
    return Env(
        data_dir=Path(os.environ["LIFE_AGENT_DATA"]),
        state_dir=Path(os.environ["LIFE_AGENT_STATE"]),
        conf_dir=Path(os.environ["LIFE_AGENT_CONF"]),
    )


@dataclass(frozen=True)
class MailConfig:
    address: str
    tag_since: date
    owner_name: str | None = None
    digest_time: str = DEFAULT_DIGEST_TIME
    timezone: str = DEFAULT_TIMEZONE
    vip_senders: list[str] = field(default_factory=list)
    vip_domains: list[str] = field(default_factory=list)
    mute_senders: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    max_needs_you: int = DEFAULT_MAX_NEEDS_YOU
    body_chars_for_model: int = DEFAULT_BODY_CHARS_FOR_MODEL


def load_config(data_dir: Path) -> MailConfig:
    """Load the `mail:` section of $LIFE_AGENT_DATA/config.yaml."""
    config_path = data_dir / "config.yaml"
    if not config_path.is_file():
        raise ConfigError(f"no config.yaml at {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}
    mail_raw = raw.get("mail")
    if not mail_raw:
        raise ConfigError(f"{config_path} has no `mail:` section")

    for required in ("address", "tag_since"):
        if required not in mail_raw:
            raise ConfigError(f"{config_path}: mail.{required} is required")

    try:
        tag_since = date.fromisoformat(str(mail_raw["tag_since"]))
    except ValueError as exc:
        raise ConfigError(
            f"{config_path}: mail.tag_since must be an ISO date (YYYY-MM-DD): {exc}"
        ) from exc

    owner_name = mail_raw.get("owner_name")

    return MailConfig(
        address=str(mail_raw["address"]),
        tag_since=tag_since,
        owner_name=str(owner_name) if owner_name else None,
        digest_time=str(mail_raw.get("digest_time", DEFAULT_DIGEST_TIME)),
        timezone=str(mail_raw.get("timezone", DEFAULT_TIMEZONE)),
        vip_senders=list(mail_raw.get("vip_senders", [])),
        vip_domains=list(mail_raw.get("vip_domains", [])),
        mute_senders=list(mail_raw.get("mute_senders", [])),
        topics=list(mail_raw.get("topics", [])),
        max_needs_you=int(mail_raw.get("max_needs_you", DEFAULT_MAX_NEEDS_YOU)),
        body_chars_for_model=int(
            mail_raw.get("body_chars_for_model", DEFAULT_BODY_CHARS_FOR_MODEL)
        ),
    )
