"""A real WebSocket client against a real daemon: handshake, framing, and dispatch."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from mcp_gateway import errors
from mcp_gateway.protocol import MCP_PROTOCOL_VERSION
from tests.fixtures.ws_client import daemon


async def test_initialize_negotiates_and_advertises(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        result = await client.initialize()
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "mcp-gateway"
        assert set(result["capabilities"]) == {"tools", "prompts", "resources"}
        assert result["capabilities"]["resources"]["subscribe"] is False
    finally:
        await harness.close()


async def test_an_older_client_gets_its_own_version_echoed(tmp_path) -> None:
    """A backend or client pinned to 2024-11-05 must be able to proceed."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        result = await client.initialize(version="2024-11-05")
        assert result["protocolVersion"] == "2024-11-05"
    finally:
        await harness.close()


async def test_an_unknown_version_is_countered_not_refused(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        result = await client.initialize(version="1999-01-01")
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    finally:
        await harness.close()


async def test_instructions_name_the_namespace_convention(tmp_path) -> None:
    """Otherwise a model has no reason to believe the `__` prefix means anything."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        instructions = (await client.initialize())["instructions"]
        assert "<server>__<tool>" in instructions
        assert "gateway__list_backends" in instructions
    finally:
        await harness.close()


async def test_ping_works_before_initialize(tmp_path) -> None:
    """A supervisor may ping before any client has spoken; refusing looks like being down."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        assert await client.call("ping") == {}
    finally:
        await harness.close()


async def test_a_method_before_initialize_is_refused_with_a_reason(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        response = await client.request("tools/list")
        assert response["error"]["code"] == errors.INVALID_REQUEST
        assert "before initialize" in response["error"]["message"]
    finally:
        await harness.close()


async def test_an_unknown_method_is_method_not_found_and_untagged(tmp_path) -> None:
    """Untagged is the point: `source` absent means the gateway said it, not a backend."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        response = await client.request("tools/nope")
        assert response["error"]["code"] == errors.METHOD_NOT_FOUND
        assert "source" not in response["error"].get("data", {})
    finally:
        await harness.close()


async def test_malformed_json_gets_a_parse_error_with_a_null_id(tmp_path) -> None:
    """There is no id to answer against, which is exactly what the spec reserves null for."""
    harness = await daemon(tmp_path)
    try:
        async with websockets.connect(harness.url()) as websocket:
            await websocket.send("{not json")
            response = json.loads(await asyncio.wait_for(websocket.recv(), 5))
        assert response["id"] is None
        assert response["error"]["code"] == errors.PARSE_ERROR
    finally:
        await harness.close()


async def test_a_non_object_payload_is_an_invalid_request(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        async with websockets.connect(harness.url()) as websocket:
            await websocket.send("[1, 2, 3]")
            response = json.loads(await asyncio.wait_for(websocket.recv(), 5))
        assert response["error"]["code"] == errors.INVALID_REQUEST
    finally:
        await harness.close()


async def test_a_notification_is_never_answered(tmp_path) -> None:
    """Answering one is a protocol violation: the peer never asked."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        await client.notify("notifications/something_unknown", {"x": 1})
        # A round trip after it: if the notification had produced a response, it would have
        # arrived before this one's, and `request` would resolve against a mismatched id.
        assert await client.call("ping") == {}
    finally:
        await harness.close()


async def test_requests_are_served_concurrently_on_one_connection(tmp_path) -> None:
    """One task per inbound message: a slow call must not block the next `ping`."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        results = await asyncio.gather(*(client.call("ping") for _ in range(10)))
        assert results == [{}] * 10
    finally:
        await harness.close()


async def test_an_unknown_path_is_refused_during_the_handshake(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(harness.url(path="/nope"))
        assert caught.value.response.status_code == 404
    finally:
        await harness.close()


async def test_several_clients_share_one_daemon(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        one = await harness.connect()
        two = await harness.connect()
        assert await one.tool_names() == await two.tool_names()
        assert len(harness.gateway.describe_connections()) == 2
    finally:
        await harness.close()


async def test_each_connection_negotiates_its_own_version(tmp_path) -> None:
    """The reason a Session is per-connection: a shared one answers with the wrong gates."""
    harness = await daemon(tmp_path)
    try:
        old = await harness.connect(initialize=False)
        new = await harness.connect(initialize=False)
        assert (await old.initialize(version="2024-11-05"))["protocolVersion"] == "2024-11-05"
        assert (await new.initialize())["protocolVersion"] == MCP_PROTOCOL_VERSION
        versions = {c["protocol_version"] for c in harness.gateway.describe_connections()}
        assert versions == {"2024-11-05", MCP_PROTOCOL_VERSION}
    finally:
        await harness.close()


async def test_a_disconnect_forgets_its_session(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        assert len(harness.gateway.describe_connections()) == 1
        await client.close()
        for _ in range(100):
            if not harness.gateway.describe_connections():
                break
            await asyncio.sleep(0.01)
        assert harness.gateway.describe_connections() == []
    finally:
        await harness.close()
