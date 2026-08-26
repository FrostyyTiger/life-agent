"""`mail serve`: a read-only query socket over the archive.

Once bootstrap (stage 8) locks `$LIFE_AGENT_STATE/mail.db` down to the `life-agent`
user, this socket is the *only* path the owner's side has into the archive — see
docs/trust-model.md. No framework: stdlib `http.server` + a `socketserver`
Unix-domain-socket server. Three endpoints, all GET, all read-only, none of which can
list the whole archive:

    GET /status                                          -> {"messages": N}
    GET /search?q=&mode=&from=&since=&until=&limit=       -> {"hits": [...]}
    GET /show?id=                                          -> {"message": {...}}

The DB is opened `mode=ro` — even a bug here cannot write to the archive.
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer
from urllib.parse import parse_qs, urlparse

from src.mail import search as search_mod
from src.mail import store

MAX_LIMIT = 50
DEFAULT_SOCKET_MODE = 0o660


class QueryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "mail-query/1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # a personal, read-only side channel; not worth per-request access logs

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        parsed = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/status":
                payload = self.server.handle_status()
            elif parsed.path == "/search":
                payload = self.server.handle_search(params)
            elif parsed.path == "/show":
                payload = self.server.handle_show(params)
            else:
                self._respond(404, {"error": f"no such endpoint: {parsed.path}"})
                return
        except QueryError as exc:
            self._respond(exc.status, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - never let an unhandled error crash the loop
            self._respond(500, {"error": str(exc)})
            return
        self._respond(200, payload)

    def _method_not_allowed(self) -> None:
        self._respond(405, {"error": "only GET is supported"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_HEAD = _method_not_allowed


class MailQueryServer(UnixStreamServer):
    def __init__(self, socket_path: Path, db_path: Path, state_dir: Path):
        self.db_path = db_path
        self.state_dir = state_dir
        self._embedder = None

        socket_path = Path(socket_path)
        if socket_path.exists():
            socket_path.unlink()
        socket_path.parent.mkdir(parents=True, exist_ok=True)

        super().__init__(str(socket_path), Handler)
        socket_path.chmod(DEFAULT_SOCKET_MODE)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _embedder_for(self, mode: str):
        if mode not in ("vec", "hybrid"):
            return None
        if self._embedder is None:
            from src.mail import embed as embed_mod

            self._embedder = embed_mod.SentenceTransformerEmbedder(self.state_dir / "models")
        return self._embedder

    def handle_status(self) -> dict:
        conn = self._connect()
        try:
            return {"messages": store.count_messages(conn)}
        finally:
            conn.close()

    def handle_search(self, params: dict) -> dict:
        query = params.get("q")
        if not query:
            raise QueryError("missing required query param: q")

        mode = params.get("mode", "fts")
        try:
            limit = min(int(params.get("limit", search_mod.DEFAULT_LIMIT)), MAX_LIMIT)
        except ValueError as exc:
            raise QueryError("limit must be an integer") from exc

        conn = self._connect()
        try:
            hits = search_mod.search(
                conn, query, mode=mode, embedder=self._embedder_for(mode),
                from_filter=params.get("from"), since=params.get("since"),
                until=params.get("until"), limit=limit,
            )
        except search_mod.SearchError as exc:
            raise QueryError(str(exc)) from exc
        finally:
            conn.close()
        return {"hits": hits}

    def handle_show(self, params: dict) -> dict:
        message_id = params.get("id")
        if not message_id:
            raise QueryError("missing required query param: id")

        conn = self._connect()
        try:
            row = store.get_message(conn, message_id)
        finally:
            conn.close()

        if row is None:
            raise QueryError(f"no message with id {message_id!r}", status=404)
        return {"message": row}


def serve(socket_path: Path, db_path: Path, state_dir: Path) -> None:
    server = MailQueryServer(socket_path, db_path, state_dir)
    try:
        server.serve_forever()
    finally:
        server.server_close()
