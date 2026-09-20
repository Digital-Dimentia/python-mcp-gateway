"""Backends reached by `url` over Streamable HTTP rather than spawned as a process.

What has to hold is that a model cannot tell the difference: the same tools under the same
names, the same notifications, the same server-to-client requests. So most of this file is
the stdio mock behind an HTTP front (`fixtures/http_backend.py`), driven through a real
daemon. The rest is what only HTTP has -- sessions that expire, headers that carry the
credential, and a server that is simply not there.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from mcp_gateway import config
from mcp_gateway.backend import BackendStatus
from mcp_gateway.naming import encode_resource_uri
from tests.fixtures.http_backend import HttpBackend
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable
ROOTS = {"roots": {"listChanged": False}}


def url_servers(url: str, extra: str = "") -> str:
    return f"""
servers:
  remote:
    url: {url}
{extra}"""


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


# --- the catalogue ---------------------------------------------------------------------------


def test_a_url_entry_parses_with_its_headers_as_templates() -> None:
    spec = config.parse(
        "servers:\n  r:\n    url: https://mcp.example/api\n"
        "    headers:\n      Authorization: 'Bearer ${TOKEN}'\n"
    ).servers["r"]
    assert spec.transport == "http"
    assert spec.url == "https://mcp.example/api"
    assert spec.headers == {"Authorization": "Bearer ${TOKEN}"}
    assert spec.header_keys == ["Authorization"]
    assert spec.command == ""


@pytest.mark.parametrize(
    ("entry", "expect_in_message"),
    [
        ("url: http://h/mcp\n    command: npx", "describe a process"),
        ("url: http://h/mcp\n    env: {A: b}", "describe a process"),
        ("url: http://h/mcp\n    cwd: /tmp", "describe a process"),
        ("command: npx\n    headers: {A: b}", "only apply to a backend with a 'url'"),
        ("url: http://h/mcp?token=${T}", "not interpolated in a url"),
        ("url: http://me:pw@h/mcp", "user:password"),
        ("url: ftp://h/mcp", "http:// or https://"),
        ("url: http:///mcp", "http:// or https://"),
        ("url: http://h:99999/mcp", "Port out of range"),
        ("url: http://h/mcp\n    headers: {Host: x}", "sets this header itself"),
        ("url: http://h/mcp\n    headers: {Mcp-Session-Id: x}", "sets this header itself"),
        ("url: http://h/mcp\n    headers: {'bad name': x}", "not a valid header name"),
        ("url: http://h/mcp\n    headers: {A: \"x\\r\\nB: y\"}", "line break"),
        ("url: http://h/mcp\n    headers: {A: x, a: y}", "named twice"),
    ],
)
def test_a_url_entry_refuses_what_it_cannot_honour(entry: str, expect_in_message: str) -> None:
    with pytest.raises(config.ConfigError) as caught:
        config.parse(f"servers:\n  r:\n    {entry}\n")
    assert expect_in_message in str(caught.value)


# --- through a running daemon ----------------------------------------------------------------


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_a_url_backends_tools_are_listed_and_called(tmp_path, sse: bool) -> None:
    async with HttpBackend({"MOCK_NAME": "far", "MOCK_TOOLS": "echo"}, sse=sse) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            backend = harness.gateway.backend("remote")
            assert backend.status is BackendStatus.RUNNING
            assert backend.pid is None
            client = await harness.connect()
            assert "remote__echo" in await client.tool_names()
            text = await client.tool_text("remote__echo", {"text": "hi"})
            assert "hi" in text
            # Everything after the handshake names the session and the negotiated revision.
            posts = far.methods("POST")
            assert "mcp-session-id" not in posts[0]
            assert all(p["mcp-session-id"] == far.session_id for p in posts[1:])
            assert all(p["mcp-protocol-version"] == backend.protocol_version for p in posts[2:])
        finally:
            await harness.close()


async def test_headers_carry_the_credential_resolved_from_gateway_env(tmp_path) -> None:
    async with HttpBackend(require_headers={"Authorization": "Bearer s3cret"}) as far:
        servers = url_servers(far.url, "    headers:\n      Authorization: 'Bearer ${FAR_TOKEN}'\n")
        harness = await daemon(tmp_path, servers=servers, env="FAR_TOKEN=s3cret\n")
        try:
            # The fixture answers 401 to anything without the header, so running is the proof.
            assert harness.gateway.backend("remote").status is BackendStatus.RUNNING
            assert all(r["authorization"] == "Bearer s3cret" for r in far.methods("POST"))
        finally:
            await harness.close()


async def test_a_missing_header_secret_fails_that_backend_by_name(tmp_path) -> None:
    async with HttpBackend() as far:
        servers = url_servers(far.url, "    headers:\n      Authorization: 'Bearer ${NOPE}'\n")
        harness = await daemon(tmp_path, servers=servers)
        try:
            backend = harness.gateway.backend("remote")
            assert backend.status is BackendStatus.FAILED
            assert "NOPE" in backend.error
            assert "servers.remote.headers.Authorization" in backend.error
            assert far.requests == []
        finally:
            await harness.close()


class LegacySseServer:
    """A server speaking the 2024-11-05 HTTP+SSE transport: GET announces, POST is refused.

    Not a working backend -- the gateway cannot talk to one of these, which is the point.
    All this has to be is recognisable, so the whole of it is the `endpoint` event the old
    transport opens with.
    """

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/sse"

    async def __aenter__(self) -> "LegacySseServer":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            verb = head.decode("latin-1").split(" ", 1)[0]
            if verb == "GET":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                    b"event: endpoint\ndata: /messages/?session_id=abc\n\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def test_a_legacy_http_sse_server_is_named_rather_than_called_unreachable(
    tmp_path,
) -> None:
    """`405, 405, 405` is what an operator gets from `curl` too. The transport is the answer."""
    async with LegacySseServer() as old:
        harness = await daemon(tmp_path, servers=url_servers(old.url, "    startup_timeout: 10\n"))
        try:
            backend = harness.gateway.backend("remote")
            assert backend.status is BackendStatus.FAILED
            assert "HTTP+SSE" in backend.error and "2024-11-05" in backend.error
            assert "Streamable HTTP proxy" in backend.error
        finally:
            await harness.close()


async def test_an_unreachable_url_fails_the_start_without_taking_the_daemon_down(
    tmp_path,
) -> None:
    async with HttpBackend() as far:
        dead = far.url  # a port that was just ours and is about to be nobody's
    harness = await daemon(tmp_path, servers=url_servers(dead, "    startup_timeout: 5\n"))
    try:
        backend = harness.gateway.backend("remote")
        assert backend.status is BackendStatus.FAILED
        assert "cannot reach" in backend.error
    finally:
        await harness.close()


async def test_a_session_the_server_forgot_is_renewed_and_the_call_still_answers(
    tmp_path,
) -> None:
    async with HttpBackend({"MOCK_TOOLS": "echo"}) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect()
            await client.tool_names()
            first = far.session_id
            far.expire()
            assert "again" in await client.tool_text("remote__echo", {"text": "again"})
            assert far.initializations == 2
            assert far.session_id not in (None, first)
            # A renewed session may be a restarted server publishing something else, so the
            # gateway is told its listings changed rather than trusting the old ones.
            await wait_for(lambda: any(
                n["method"] == "notifications/tools/list_changed" for n in client.notifications
            ))
        finally:
            await harness.close()


async def test_a_renewed_session_gets_the_subscriptions_and_the_log_level_back(
    tmp_path,
) -> None:
    """Both belonged to the session the server forgot, and only the gateway knows them.

    A subscription that silently stopped producing looks exactly like a resource that
    stopped changing, and a client that asked for `debug` an hour ago is not going to ask
    again -- so a renewal that did not replay these would be a leak nobody could see. What
    proves it is the far end's own record of what it was asked, after the renewal.
    """
    env = {
        "MOCK_TOOLS": "echo",
        "MOCK_RESOURCES": "file:///README.md",
        "MOCK_SUBSCRIBE": "1",
        "MOCK_LOGGING": "1",
    }
    async with HttpBackend(env) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect()
            public = encode_resource_uri("remote", "file:///README.md")
            assert await client.call("resources/subscribe", {"uri": public}) == {}
            assert await client.call("logging/setLevel", {"level": "debug"}) == {}
            far.received.clear()

            far.expire()
            assert "again" in await client.tool_text("remote__echo", {"text": "again"})
            await wait_for(lambda: "resources/subscribe" in far.received)
            await wait_for(lambda: "logging/setLevel" in far.received)
        finally:
            await harness.close()


async def test_calls_that_find_the_session_gone_together_renew_it_once(tmp_path) -> None:
    async with HttpBackend({"MOCK_TOOLS": "echo"}) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect()
            await client.tool_names()
            far.expire()
            texts = await asyncio.gather(
                *(client.tool_text("remote__echo", {"text": f"n{i}"}) for i in range(3))
            )
            assert all(f"n{i}" in text for i, text in enumerate(texts))
            assert far.initializations == 2, "one lost session, one renewal"
        finally:
            await harness.close()


async def test_unprompted_notifications_arrive_on_the_get_stream(tmp_path) -> None:
    env = {"MOCK_LIST_CHANGED_AFTER_MS": "300"}
    async with HttpBackend(env) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect()
            await client.tool_names()
            await wait_for(lambda: any(
                n["method"] == "notifications/tools/list_changed" for n in client.notifications
            ))
            assert far.methods("GET"), "the gateway never opened the GET stream"
        finally:
            await harness.close()


async def test_a_server_with_no_get_stream_still_serves_calls(tmp_path) -> None:
    async with HttpBackend({"MOCK_TOOLS": "echo"}, get_stream=False) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect()
            assert "ok" in await client.tool_text("remote__echo", {"text": "ok"})
            await asyncio.sleep(0.2)
            assert len(far.methods("GET")) == 1, "a 405 is final, not something to retry"
        finally:
            await harness.close()


@pytest.mark.parametrize("sse", [False, True], ids=["get-stream", "post-stream"])
async def test_a_url_backends_roots_request_reaches_the_calling_client(
    tmp_path, sse: bool
) -> None:
    env = {"MOCK_TOOLS": "ask", "MOCK_ASK_ROOTS": "1"}
    async with HttpBackend(env, sse=sse) as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        try:
            client = await harness.connect(capabilities=ROOTS)
            await harness.gateway.restart_backend("remote")
            client.answer_with = {"roots": [{"uri": "file:///work", "name": "work"}]}
            answered = json.loads(await client.tool_text("remote__ask", {}))
            assert answered["roots"]["roots"][0]["uri"] == "file:///work"
        finally:
            await harness.close()


async def test_stopping_a_url_backend_ends_its_session(tmp_path) -> None:
    async with HttpBackend() as far:
        harness = await daemon(tmp_path, servers=url_servers(far.url))
        session = far.session_id
        await harness.close()
        deletes = far.methods("DELETE")
        assert [d["mcp-session-id"] for d in deletes] == [session]


async def test_a_gateway_can_be_another_gateways_backend(tmp_path) -> None:
    """The realest server to hand: this project's own `/mcp`, which answers in JSON, mints
    sessions, holds a GET stream and honours DELETE."""
    upstream = await daemon(
        tmp_path / "up",
        servers=f"""
servers:
  zoo:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "zoo"
      MOCK_TOOLS: "echo"
""",
    )
    try:
        url = f"http://127.0.0.1:{upstream.gateway.server.port}/mcp"
        downstream = await daemon(tmp_path / "down", servers=url_servers(url))
        try:
            client = await downstream.connect()
            assert "remote__zoo__echo" in await client.tool_names()
            assert "through" in await client.tool_text("remote__zoo__echo", {"text": "through"})
        finally:
            await downstream.close()
    finally:
        await upstream.close()


async def test_list_prints_the_url_and_header_names_but_never_a_value(tmp_path, capsys) -> None:
    from mcp_gateway import cli

    (tmp_path / "servers.yaml").write_text(
        url_servers("http://127.0.0.1:9/mcp", "    headers:\n      X-Api-Key: '${K}'\n")
    )
    (tmp_path / "gateway.env").write_text("K=do-not-print-me\n")
    cli.check(tmp_path / "servers.yaml", tmp_path / "gateway.env", list_plan=True)
    err = capsys.readouterr().err
    assert "url: http://127.0.0.1:9/mcp" in err
    assert "header keys: X-Api-Key" in err
    assert "do-not-print-me" not in err
