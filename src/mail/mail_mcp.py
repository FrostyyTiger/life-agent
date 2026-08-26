"""`mail mcp`: a stdio MCP server exposing `search`/`show`/`status` — a thin wrapper
over the read-only query socket (`serve.py`), nothing more. Runs as the owner, not
`life-agent`; the point of stage 10 is letting any Claude Code session on the host
*opt into* mail access by registering this in `~/.claude.json`, while still only ever
reaching the archive through the socket, never `mail.db` directly. Not registered in
any project's settings — a session that wants mail asks for it explicitly.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.mail import socket_client

DEFAULT_SOCKET_PATH = Path("/run/life-agent/mail.sock")


def build_server(socket_path: Path):
    # Lazy import: the `mcp` package (pydantic, starlette, ...) is only needed for this
    # one command, not for every `mail` invocation — same reasoning as embed.py/gmail.py
    # keeping torch/google imports inside their own functions.
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "life-agent-mail",
        instructions=(
            "Search and read the owner's mail archive. Read-only: there is no way to "
            "send, delete, or modify anything through this server. `search` supports "
            "fts (keyword), vec (semantic), and hybrid modes."
        ),
    )

    def _query(path: str, params: dict | None = None) -> dict:
        # Errors come back as data ({"error": "..."}), not a raised exception — a
        # missing id or an empty query is an ordinary result for a model to see and
        # react to, not a crash. Only a genuinely unreachable socket is exceptional,
        # and socket_client already raises a clear SocketQueryError for that case,
        # which still ends up here rather than propagating as a bare tool crash.
        try:
            return socket_client.get(socket_path, path, params)
        except socket_client.SocketQueryError as exc:
            return {"error": str(exc)}

    @server.tool()
    def status() -> dict:
        """Message count and basic health of the mail archive."""
        return _query("/status")

    @server.tool()
    def search(
        q: str,
        mode: str = "fts",
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Search the mail archive. `mode` is one of fts, vec, hybrid. `sender` filters
        by from-address substring. `since`/`until` are YYYY-MM or YYYY-MM-DD.
        """
        return _query(
            "/search",
            {"q": q, "mode": mode, "from": sender, "since": since, "until": until, "limit": limit},
        )

    @server.tool()
    def show(id: str) -> dict:
        """Show one message's headers and body text by id (from a `search` result)."""
        return _query("/show", {"id": id})

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mail-mcp")
    parser.add_argument("--socket", default=str(DEFAULT_SOCKET_PATH))
    args = parser.parse_args(argv)

    server = build_server(Path(args.socket))
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
