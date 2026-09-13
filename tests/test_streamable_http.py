"""Streamable HTTP on the same port as the WebSocket transport.

The point of the feature is that a client which speaks HTTP -- Claude Code among them --
attaches with no bridge in between, so most of this file is an HTTP client doing what such a
client does. The other half is the part that could go wrong quietly: that adding a second
transport did not weaken the first, or the access-key check, or the UI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_gateway import transport_http
from tests.fixtures.http_client import HttpClient
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  zoo:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "zoo"
      MOCK_RESOURCES: "file:///README.md"
      MOCK_SUBSCRIBE: "1"
      MOCK_UPDATE_ON_SUBSCRIBE: "1"
      MOCK_LOGGING: "1"
"""


def client_for(harness, **kwargs) -> HttpClient:
    return HttpClient(harness.gateway.server.port, **kwargs)


# --- parsing and framing, with no socket ---------------------------------------------------


def test_a_request_head_parses_into_its_parts() -> None:
    head = transport_http.parse_head(
        b"POST /mcp?key=abc HTTP/1.1\r\nHost: x\r\nContent-Length: 12\r\nAccept: application/json"
    )
    assert head is not None
    assert (head.method, head.path, head.content_length) == ("POST", "/mcp", 12)
    assert head.get("host") == "x"


def test_header_names_are_matched_without_regard_to_case() -> None:
    """Clients differ on `Mcp-Session-Id` vs `MCP-Session-ID`, and HTTP says both are one."""
    head = transport_http.parse_head(b"GET /mcp HTTP/1.1\r\nMCP-SESSION-ID: abc\r\n")
    assert head is not None and head.get("mcp-session-id") == "abc"


def test_a_repeated_header_is_joined_rather_than_lost() -> None:
    head = transport_http.parse_head(b"GET /mcp HTTP/1.1\r\nAccept: a\r\nAccept: b\r\n")
    assert head is not None and head.get("accept") == "a, b"


def test_something_that_is_not_http_is_refused_rather_than_guessed() -> None:
    assert transport_http.parse_head(b"hello there\r\n") is None
    assert transport_http.parse_head(b"GET /mcp HTTP/1.1\r\nnot a header\r\n") is None


def test_accept_is_forgiving_about_a_missing_or_wildcard_header() -> None:
    """A client sending neither type is far likelier to be old than hostile."""
    bare = transport_http.parse_head(b"POST /mcp HTTP/1.1\r\n")
    assert bare is not None and bare.accepts("application/json")
    star = transport_http.parse_head(b"POST /mcp HTTP/1.1\r\nAccept: */*\r\n")
    assert star is not None and star.accepts("text/event-stream")
    narrow = transport_http.parse_head(b"POST /mcp HTTP/1.1\r\nAccept: text/plain\r\n")
    assert narrow is not None and not narrow.accepts("application/json")


def test_an_sse_frame_prefixes_every_line_of_its_data() -> None:
    """A writer that splits on the wrong thing produces a stream that parses as valid and
    means something else, which is the worst failure this module could have."""
    assert transport_http.sse_frame("one", 3) == b"id: 3\ndata: one\n\n"
    assert transport_http.sse_frame("a\nb") == b"data: a\ndata: b\n\n"


def test_a_session_id_is_unguessable() -> None:
    """It is a bearer token: whoever holds it can drive the session."""
    ids = {transport_http.new_session_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 30 for i in ids)


# --- the conversation ------------------------------------------------------------------------


async def test_initialize_mints_a_session_and_tools_can_be_called(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        response = await client.initialize()
        assert response.status == 200
        assert response.session_id
        assert response.json()["id"] == 1
        assert response.json()["result"]["serverInfo"]["name"] == "mcp-gateway"

        names = [t["name"] for t in (await client.result("tools/list"))["tools"]]
        assert "zoo__echo" in names
        called = await client.result("tools/call", {"name": "zoo__echo", "arguments": {"text": "hi"}})
        assert called["content"][0]["text"] == "zoo:echo:hi"
    finally:
        await harness.close()


async def test_a_notification_is_accepted_with_no_body(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        response = await client.notify("notifications/initialized")
        assert response.status == 202
        assert response.body == b""
    finally:
        await harness.close()


async def test_a_request_without_a_session_is_404_not_a_new_session(tmp_path) -> None:
    """The spec's way of saying "start again", and the honest answer after a restart.
    Inventing one would let a client believe a subscription survived that did not."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        assert (await client.rpc("tools/list", session=None)).status == 404
        assert (await client.rpc("tools/list", session="made-up")).status == 404
    finally:
        await harness.close()


async def test_malformed_json_is_a_jsonrpc_error_not_a_400(tmp_path) -> None:
    """A parse error *is* the answer to a well-formed HTTP request. Reporting it as 400
    would put it somewhere a JSON-RPC client is not looking."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        response = await client.send("POST", b"{not json")
        assert response.status == 200
        assert response.json()["error"]["code"] == -32700
    finally:
        await harness.close()


async def test_an_unknown_method_gets_its_error_over_http(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        response = await client.rpc("tools/nope")
        assert response.status == 200
        assert response.json()["error"]["code"] == -32601
    finally:
        await harness.close()


async def test_delete_ends_the_session(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        assert (await client.send("DELETE")).status == 204
        assert (await client.rpc("tools/list")).status == 404
    finally:
        await harness.close()


async def test_an_unsupported_method_says_which_are(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        response = await client_for(harness).send("PUT")
        assert response.status == 405
    finally:
        await harness.close()


# --- the stream --------------------------------------------------------------------------------


async def test_the_get_stream_carries_a_notification(tmp_path) -> None:
    """A subscription's `resources/updated` has nowhere else to go: the POST that asked for
    it has long since been answered."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        async with client.stream() as stream:
            assert stream.status == 200
            assert stream.headers["content-type"] == "text/event-stream"
            uri = (await client.result("resources/list"))["resources"][0]["uri"]
            await client.result("resources/subscribe", {"uri": uri})
            assert await stream.wait_for(lambda: stream.of_kind("notifications/resources/updated"))
            updated = stream.of_kind("notifications/resources/updated")[0]
            assert updated["params"]["uri"] == uri
    finally:
        await harness.close()


async def test_a_notification_raised_before_the_stream_opens_is_not_lost(tmp_path) -> None:
    """`initialize` happens before any GET can, so a link with no stream yet has to hold
    what it is given rather than drop it."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        await client.result("logging/setLevel", {"level": "debug"})
        await client.result(
            "tools/call", {"name": "zoo__log", "arguments": {"level": "error", "text": "early"}}
        )
        async with client.stream() as stream:
            assert await stream.wait_for(lambda: stream.of_kind("notifications/message"))
            assert stream.of_kind("notifications/message")[0]["params"]["data"] == "early"
    finally:
        await harness.close()


async def test_a_stream_without_a_session_is_404(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        async with client.stream() as stream:
            assert stream.status == 404
    finally:
        await harness.close()


async def test_the_session_survives_the_stream_reconnecting(tmp_path) -> None:
    """A laptop sleeping must not cost a client its subscriptions. That is what the grace
    on `reap` buys, and it is the reason sessions are not tied to the GET connection."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        uri = (await client.result("resources/list"))["resources"][0]["uri"]
        async with client.stream():
            await client.result("resources/subscribe", {"uri": uri})
        # The stream is gone; the session, and its subscription, are not.
        assert harness.gateway.subscriptions.sessions_for(uri) != []
        async with client.stream() as second:
            assert second.status == 200
            assert (await client.rpc("ping")).status == 200
    finally:
        await harness.close()


# --- not breaking what was already there -------------------------------------------------------


async def test_the_websocket_transport_still_works_beside_it(tmp_path) -> None:
    """The whole risk of sharing a port. A WebSocket handshake is never diverted, so the
    library keeps every line of its own handling."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        http = client_for(harness)
        await http.initialize()
        ws = await harness.connect()
        assert await ws.tool_text("zoo__echo", {"text": "ws"}) == "zoo:echo:ws"
        assert (await http.result("tools/call", {"name": "zoo__echo", "arguments": {"text": "http"}}))[
            "content"
        ][0]["text"] == "zoo:echo:http"
    finally:
        await harness.close()


async def test_the_ui_is_still_served(tmp_path) -> None:
    """`/ui` is not diverted, so it keeps the single `process_request` implementation."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        response = await client_for(harness).send("GET", path="/ui/", accept=None)
        assert response.status == 200
        assert b"<!doctype html>" in response.body.lower()
    finally:
        await harness.close()


async def test_an_unknown_path_is_still_404(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        response = await client_for(harness).send("GET", path="/nope", accept=None)
        assert response.status == 404
    finally:
        await harness.close()


async def test_both_transports_appear_in_the_connection_list(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        http = client_for(harness)
        await http.initialize()
        await harness.connect()
        paths = sorted(c["path"] for c in harness.gateway.describe_connections())
        assert paths == ["/mcp", "/mcp"]
    finally:
        await harness.close()


async def test_a_broadcast_reaches_an_http_client_too(tmp_path) -> None:
    """`list_changed` goes to every `/mcp` connection, and an HTTP session is one."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        async with client.stream() as stream:
            await harness.gateway.restart_backend("zoo")
            # Waited for rather than flushed: `Notifier.flush` cancels pending emissions,
            # so forcing one here would test the opposite of what happens in a daemon.
            assert await stream.wait_for(
                lambda: stream.of_kind("notifications/tools/list_changed")
            )
    finally:
        await harness.close()


# --- the access key ---------------------------------------------------------------------------


async def test_the_access_key_is_required_on_http_too(tmp_path) -> None:
    """A transport with its own weaker copy of an auth rule is how a gateway grows a back
    door, so both paths share `origin_permitted` and the constant-time comparison."""
    harness = await daemon(tmp_path, servers=SERVERS, access_key="s3cret")
    try:
        assert (await client_for(harness).rpc("ping", session=None)).status == 401
        assert (await client_for(harness, key="wrong").rpc("ping", session=None)).status == 401
        allowed = client_for(harness, key="s3cret")
        assert (await allowed.initialize()).status == 200
    finally:
        await harness.close()


async def test_a_bearer_token_is_accepted_like_the_websocket_path(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, access_key="s3cret")
    try:
        client = client_for(harness)
        response = await client.initialize(headers={"Authorization": "Bearer s3cret"})
        assert response.status == 200
    finally:
        await harness.close()


async def test_two_keys_cannot_be_smuggled_past_the_check(tmp_path) -> None:
    """`?key=wrong&key=right` must not pass a check that scanned for any match."""
    harness = await daemon(tmp_path, servers=SERVERS, access_key="s3cret")
    try:
        client = HttpClient(harness.gateway.server.port)
        response = await client.send(
            "POST",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
            path="/mcp?key=wrong&key=s3cret",
            session=None,
        )
        assert response.status == 401
    finally:
        await harness.close()


async def test_a_disallowed_origin_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        response = await client_for(harness).rpc(
            "ping", headers={"Origin": "https://evil.example"}, session=None
        )
        assert response.status == 403
    finally:
        await harness.close()


async def test_an_expired_session_is_reaped(tmp_path) -> None:
    """A client that vanishes without a DELETE is the ordinary case, not an exception."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = client_for(harness)
        await client.initialize()
        endpoint = harness.gateway.server.http
        assert len(endpoint.links) == 1
        endpoint._grace = -1.0  # every idle session is now past its deadline
        for session_id in endpoint.reap():
            await endpoint.drop(session_id)
        assert endpoint.links == frozenset()
        assert (await client.rpc("ping")).status == 404
    finally:
        await harness.close()
