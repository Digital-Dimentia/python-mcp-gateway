"""The stdio<->WS bridge, driven as a real subprocess against a real daemon.

Spawned rather than called, because the two things that matter -- that stdout carries
nothing but protocol, and that the process survives a daemon restart -- are properties of a
process, not of a coroutine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

from mcp_gateway import errors
from mcp_gateway.bridge import Bridge, build_parser, connect_kwargs, resolve_key, resolve_url
from mcp_gateway.transport_ws import ACCESS_KEY_ENV, ACCESS_KEY_SECRET_NAME
from tests.fixtures.ws_client import daemon

KEY = "bridge-test-key"


class BridgeProcess:
    """The real `mcp-gateway-connect`, speaking newline-delimited JSON on a pipe."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self._id = 0

    async def request(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        assert line, "bridge closed stdout"
        return json.loads(line)

    async def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()

    async def send_raw(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        await self.proc.stdin.drain()

    async def read_line(self, timeout: float = 10.0) -> dict:
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        return json.loads(line)

    async def close(self) -> None:
        if self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:  # pragma: no cover - only if the bridge wedges
                self.proc.kill()
                await self.proc.wait()


async def bridge_process(url: str, *, key: str | None = None, reconnect: bool = False):
    args = [sys.executable, "-m", "mcp_gateway.bridge", "--url", url]
    if not reconnect:
        args.append("--no-reconnect")
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    if key:
        env[ACCESS_KEY_ENV] = key
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    return BridgeProcess(proc)


# --- resolution -------------------------------------------------------------------------


def test_url_precedence_is_flag_then_environment_then_default() -> None:
    parser = build_parser()
    assert resolve_url(parser.parse_args(["--url", "ws://a/mcp"]), {}) == "ws://a/mcp"
    assert resolve_url(parser.parse_args([]), {"MCP_GATEWAY_URL": "ws://b/mcp"}) == "ws://b/mcp"
    assert resolve_url(parser.parse_args([]), {}) == "ws://127.0.0.1:8765/mcp"


def test_the_key_goes_in_a_header_not_the_url() -> None:
    """The carrier that does not end up in an access log."""
    kwargs = connect_kwargs(KEY)
    assert kwargs["additional_headers"]["Authorization"] == f"Bearer {KEY}"


def test_no_key_sends_no_header() -> None:
    assert connect_kwargs(None)["additional_headers"] == {}


def test_the_key_is_read_from_a_named_credential_store(tmp_path) -> None:
    env_path = tmp_path / "gateway.env"
    env_path.write_text(f"{ACCESS_KEY_SECRET_NAME}={KEY}\n")
    args = build_parser().parse_args(["--env", str(env_path)])
    assert resolve_key(args, {}) == KEY


def test_a_credential_store_is_not_guessed_from_the_cwd() -> None:
    """A bridge is spawned from an arbitrary directory; guessing would be guessing."""
    assert resolve_key(build_parser().parse_args([]), {}) is None


def test_the_environment_wins_over_the_store(tmp_path) -> None:
    env_path = tmp_path / "gateway.env"
    env_path.write_text(f"{ACCESS_KEY_SECRET_NAME}=from-file\n")
    args = build_parser().parse_args(["--env", str(env_path)])
    assert resolve_key(args, {ACCESS_KEY_ENV: "from-env"}) == "from-env"


# --- against a real daemon ---------------------------------------------------------------


async def test_a_full_handshake_through_the_bridge(tmp_path) -> None:
    harness = await daemon(tmp_path, access_key=KEY)
    bridge = await bridge_process(harness.url(key=None), key=KEY)
    try:
        response = await bridge.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        assert response["result"]["serverInfo"]["name"] == "mcp-gateway"
        await bridge.notify("notifications/initialized")
        tools = await bridge.request("tools/list")
        assert "gateway__list_backends" in [t["name"] for t in tools["result"]["tools"]]
    finally:
        await bridge.close()
        await harness.close()


async def test_every_byte_on_stdout_is_exactly_one_jsonrpc_message(tmp_path) -> None:
    """The property that makes the bridge safe. One stray byte desynchronises the client."""
    harness = await daemon(tmp_path)
    bridge = await bridge_process(harness.url(key=None))
    try:
        await bridge.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        await bridge.notify("notifications/initialized")
        for _ in range(5):
            await bridge.request("ping")
        bridge.proc.stdin.close()
        stdout, _ = await asyncio.wait_for(bridge.proc.communicate(), 10)
        for line in stdout.decode().splitlines():
            if line.strip():
                parsed = json.loads(line)
                assert parsed["jsonrpc"] == "2.0"
    finally:
        await bridge.close()
        await harness.close()


async def test_a_tool_call_round_trips(tmp_path) -> None:
    harness = await daemon(tmp_path)
    bridge = await bridge_process(harness.url(key=None))
    try:
        await bridge.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        await bridge.notify("notifications/initialized")
        response = await bridge.request(
            "tools/call", {"name": "gateway__list_backends", "arguments": {}}
        )
        assert "content" in response["result"]
    finally:
        await bridge.close()
        await harness.close()


async def test_unparseable_input_is_answered_without_reaching_the_daemon(tmp_path) -> None:
    """Answered here, where the offending line is in hand, rather than forwarded."""
    harness = await daemon(tmp_path)
    bridge = await bridge_process(harness.url(key=None))
    try:
        await bridge.send_raw("{not json")
        response = await bridge.read_line()
        assert response["error"]["code"] == errors.PARSE_ERROR
        assert response["id"] is None
    finally:
        await bridge.close()
        await harness.close()


async def test_stdin_eof_ends_the_bridge(tmp_path) -> None:
    harness = await daemon(tmp_path)
    bridge = await bridge_process(harness.url(key=None))
    try:
        bridge.proc.stdin.close()
        await asyncio.wait_for(bridge.proc.wait(), 10)
        assert bridge.proc.returncode == 0
    finally:
        await bridge.close()
        await harness.close()


async def test_a_daemon_that_goes_away_is_survived_and_reconnected(tmp_path) -> None:
    """The reason the bridge does not exit: `claude mcp` would mark the server dead.

    A request sent during the outage **waits** rather than failing. That is deliberate: a
    daemon restart takes about a second, and failing a call the client could simply have
    waited out would surface as a broken tool for no reason.
    """
    harness = await daemon(tmp_path)
    port = harness.port
    bridge = await bridge_process(f"ws://127.0.0.1:{port}/mcp", reconnect=True)
    try:
        await bridge.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )
        await harness.close()
        await asyncio.sleep(0.2)
        assert bridge.proc.returncode is None, "the bridge must not have exited"

        # Put a daemon back on the same port; the bridge finds it again, unaided.
        revived = await daemon(tmp_path / "again", host="127.0.0.1", port=port)
        try:
            response = await bridge.request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
                timeout=20,
            )
            assert response["result"]["serverInfo"]["name"] == "mcp-gateway"
        finally:
            await revived.close()
    finally:
        await bridge.close()


async def test_a_request_sent_to_a_daemon_that_never_comes_back_is_failed(tmp_path) -> None:
    """The other half: waiting forever is not an option either."""
    from mcp_gateway import bridge as bridge_module

    bridge = Bridge("ws://127.0.0.1:1/mcp", None, reconnect=False)
    written: list = []
    bridge.write = written.append  # type: ignore[method-assign]
    original, bridge_module._CONNECT_WAIT_SECONDS = bridge_module._CONNECT_WAIT_SECONDS, 0.05
    try:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}\n')
        reader.feed_eof()
        with contextlib.suppress(Exception):
            await bridge._stdin_to_socket(reader)
    finally:
        bridge_module._CONNECT_WAIT_SECONDS = original
    assert written[0]["id"] == 7
    assert written[0]["error"]["data"]["bridge"] is True


async def test_an_in_flight_request_is_failed_rather_than_left_hanging(tmp_path) -> None:
    bridge = Bridge("ws://127.0.0.1:1/mcp", None, reconnect=False)
    written: list = []
    bridge.write = written.append  # type: ignore[method-assign]
    bridge._in_flight.update({1, 2})
    bridge.fail_in_flight("daemon closed the connection")
    assert [m["id"] for m in written] == [1, 2]
    assert all(m["error"]["data"]["bridge"] for m in written)
    assert bridge._in_flight == set()
