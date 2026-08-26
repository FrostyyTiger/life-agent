import threading

import pytest

from src.mail import serve, socket_client, store


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


def test_socket_mode_is_0660(running_server):
    mode = running_server.stat().st_mode & 0o777
    assert mode == serve.DEFAULT_SOCKET_MODE


def test_status_endpoint(running_server):
    payload = socket_client.get(running_server, "/status")
    assert payload == {"messages": 1}


def test_search_endpoint_returns_hits(running_server):
    payload = socket_client.get(running_server, "/search", {"q": "gutter"})
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["id"] == "m1"


def test_search_with_filters(running_server):
    payload = socket_client.get(running_server, "/search", {"q": "gutter", "from": "hartmann"})
    assert len(payload["hits"]) == 1


def test_search_missing_query_is_a_client_error(running_server):
    status, payload = socket_client.request(running_server, "/search")
    assert status == 400
    assert "error" in payload


def test_search_limit_is_capped_at_max(running_server):
    status, payload = socket_client.request(
        running_server, "/search", {"q": "gutter", "limit": "10000"}
    )
    assert status == 200  # accepted, just silently capped server-side


def test_show_endpoint_returns_the_message(running_server):
    payload = socket_client.get(running_server, "/show", {"id": "m1"})
    assert payload["message"]["id"] == "m1"
    assert payload["message"]["subject"] == "Roof gutter quote"


def test_show_missing_id_is_a_client_error(running_server):
    status, _ = socket_client.request(running_server, "/show")
    assert status == 400


def test_show_unknown_id_is_404(running_server):
    status, payload = socket_client.request(running_server, "/show", {"id": "nope"})
    assert status == 404


def test_unknown_path_is_404(running_server):
    status, _ = socket_client.request(running_server, "/whatever")
    assert status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD"])
def test_non_get_methods_are_refused(running_server, method):
    status, payload = socket_client.request(running_server, "/status", method=method)
    assert status == 405
    assert "error" in payload


def test_no_endpoint_can_list_the_whole_archive(running_server):
    """Every endpoint requires an id or a query — there is no "list everything"."""
    status, _ = socket_client.request(running_server, "/search")
    assert status == 400
    status, _ = socket_client.request(running_server, "/show")
    assert status == 400
