"""A minimal HTTP-over-Unix-socket client for `serve.py`'s query socket — no
dependency beyond the standard library, since this is a handful of GET requests.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import urlencode

DEFAULT_TIMEOUT = 5.0


class SocketQueryError(Exception):
    pass


def request(
    socket_path: Path, path: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
    method: str = "GET",
) -> tuple[int, dict]:
    """Low-level: returns (status_code, payload) without raising on a 4xx/5xx status.
    `get()` below is what production code should use; this exists so tests can assert
    on the status code directly (e.g. a non-GET method being refused).
    """
    query = f"?{urlencode({k: v for k, v in (params or {}).items() if v is not None})}" if params else ""
    http_request = f"{method} {path}{query} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(http_request.encode("utf-8"))

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise SocketQueryError(f"could not reach {socket_path}: {exc}") from exc
    finally:
        sock.close()

    response = b"".join(chunks)
    header_blob, _, body = response.partition(b"\r\n\r\n")
    if not header_blob:
        raise SocketQueryError(f"empty response from {socket_path}")

    status_line = header_blob.split(b"\r\n", 1)[0]
    try:
        status_code = int(status_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise SocketQueryError(f"malformed response from {socket_path}: {status_line!r}") from exc

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise SocketQueryError(f"non-JSON response from {socket_path}: {body[:200]!r}") from exc

    return status_code, payload


def get(socket_path: Path, path: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    status_code, payload = request(socket_path, path, params, timeout)
    if status_code >= 400:
        raise SocketQueryError(payload.get("error", f"HTTP {status_code}"))
    return payload
