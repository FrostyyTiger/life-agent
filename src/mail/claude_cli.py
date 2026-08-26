"""Runs `claude -p` for structured judgement calls (tagging, digest composition).

No tools, no session persistence, no hooks/CLAUDE.md discovery (`--bare`) — the model
in this path reads untrusted mail content and must not be able to act on anything, only
answer a schema-shaped question. `cwd` and `HOME` point at empty, dedicated
directories under `$LIFE_AGENT_STATE` so nothing is written into anyone's real home;
the OAuth token is read from a file and injected as an env var, never passed as a CLI
argument (which would put it in `ps` output and any process-list logging).

`claude` is invoked by bare name, resolved via `PATH` — never a hardcoded path. The
owner's nvm tree is where it actually lives, which is host-specific and does not belong
in this public repo; the systemd unit (stage 8) sets `PATH` accordingly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

CLAUDE_BINARY = "claude"
DEFAULT_TIMEOUT_SECONDS = 120
TOKEN_FILENAME = "claude-oauth-token"


class ClaudeCliError(Exception):
    pass


def _read_token(conf_dir: Path) -> str:
    token_path = conf_dir / TOKEN_FILENAME
    if not token_path.is_file():
        raise ClaudeCliError(
            f"no token at {token_path} — NEED-MARCEL: run `claude setup-token` as the "
            f"owner and save the result there, mode 0600"
        )
    return token_path.read_text().strip()


def run(
    prompt: str,
    *,
    model: str,
    json_schema: dict,
    conf_dir: Path,
    state_dir: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Runs one `claude -p` call and returns the schema-shaped result as a dict."""
    import os

    token = _read_token(conf_dir)

    cwd = state_dir / "claude-cwd"
    home = state_dir / "home"
    cwd.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    command = [
        CLAUDE_BINARY,
        "-p",
        "--bare",
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(json_schema),
    ]

    try:
        proc = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude -p timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCliError(f"'{CLAUDE_BINARY}' not found on PATH") from exc

    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude -p exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:2000]}"
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(
            f"claude -p produced invalid JSON: {proc.stdout[:500]!r}"
        ) from exc

    if envelope.get("is_error"):
        raise ClaudeCliError(f"claude -p reported an error: {envelope.get('result')!r}")

    result = envelope.get("result")
    if isinstance(result, str):
        # --json-schema's structured output has, in every case observed so far, arrived
        # as a JSON string inside `result` rather than a nested object — verify this
        # once real auth exists (NEED-MARCEL item 2); this branch is what makes either
        # shape work without code changes if it turns out to already be a dict.
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ClaudeCliError(
                f"claude -p's result was not valid JSON: {result[:500]!r}"
            ) from exc

    if not isinstance(result, dict):
        raise ClaudeCliError(f"claude -p's result was not a JSON object: {result!r}")

    return result
