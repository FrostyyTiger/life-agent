import asyncio
import json
import threading

import pytest

from src.mail import mail_mcp, serve, store


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.is_error is False
    return json.loads(result.content[0].text)


@pytest.fixture
def running_server(tmp_path, message_factory):
    db_path = tmp_path / "mail.db"
    conn = store.connect(db_path)
    store.upsert_message(conn, message_factory("m1", subject="Roof gutter quote",
                                                from_addr="hartmann@example.com"))
    conn.close()

    socket_path = tmp_path / "mail.sock"
    server = serve.MailQueryServer(socket_path, db_path, tmp_path / "state")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield socket_path

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_exposes_exactly_search_show_status(running_server):
    server = mail_mcp.build_server(running_server)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"search", "show", "status"}


def test_status_tool(running_server):
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "status", {})
    assert payload == {"messages": 1}


def test_search_tool(running_server):
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "search", {"q": "gutter"})
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["id"] == "m1"


def test_search_tool_with_sender_filter(running_server):
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "search", {"q": "gutter", "sender": "hartmann"})
    assert len(payload["hits"]) == 1
    payload = _call(server, "search", {"q": "gutter", "sender": "nobody-else"})
    assert payload["hits"] == []


def test_search_tool_respects_limit_default(running_server):
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "search", {"q": "gutter", "limit": 1})
    assert len(payload["hits"]) <= 1


def test_show_tool(running_server):
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "show", {"id": "m1"})
    assert payload["message"]["subject"] == "Roof gutter quote"


def test_show_tool_unknown_id_is_an_error(running_server):
    # Reported as data ({"error": ...}), not a raised/crashing tool call — see
    # mail_mcp.py's _query() docstring for why.
    server = mail_mcp.build_server(running_server)
    payload = _call(server, "show", {"id": "nope"})
    assert "error" in payload


def test_server_is_read_only_no_write_tools_exist(running_server):
    """Stage 10 is a thin wrapper over the read-only socket — confirm nothing beyond
    the three read-only tools ever gets exposed, even by accident."""
    server = mail_mcp.build_server(running_server)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert not (names - {"search", "show", "status"})
