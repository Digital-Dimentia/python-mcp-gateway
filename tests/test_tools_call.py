"""Forwarding a tool call, and forwarding what comes back without reshaping it."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_gateway import errors
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  alpha:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "alpha"
      MOCK_TOOLS: "echo,other"
      MOCK_ERROR_ON_CALL: "other"
  beta:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "beta"
      MOCK_TOOLS: "echo"
      MOCK_ISERROR_ON_CALL: "echo"
"""


async def test_a_call_reaches_the_right_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert await client.tool_text("alpha__echo", {"text": "hi"}) == "alpha:echo:hi"
    finally:
        await harness.close()


async def test_identically_named_tools_go_to_different_backends(tmp_path) -> None:
    """What the namespacing is for: the prefix is the routing, not decoration."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert (await client.tool_text("alpha__echo", {"text": "x"})).startswith("alpha:")
        result = await client.call("tools/call", {"name": "beta__echo", "arguments": {}})
        assert result["isError"] is True  # beta's echo is configured to fail
    finally:
        await harness.close()


async def test_a_backends_iserror_result_is_forwarded_as_a_result(tmp_path) -> None:
    """Tool-level failure is a *successful* result. Converting it would lose `isError`."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "beta__echo", "arguments": {}})
        assert "error" not in response
        assert response["result"]["isError"] is True
    finally:
        await harness.close()


async def test_a_backends_jsonrpc_error_keeps_its_code_and_is_tagged(tmp_path) -> None:
    """The `source` discriminator: a client must be able to tell whose error this was."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "alpha__other", "arguments": {}})
        error = response["error"]
        assert error["code"] == -32042
        assert "refused by alpha" in error["message"]
        assert error["data"]["source"] == errors.BACKEND_SOURCE
        assert error["data"]["backend"] == "alpha"
        assert error["data"]["mcpCode"] == -32042
        assert error["data"]["mcpData"] == {"tool": "other"}
    finally:
        await harness.close()


async def test_an_unknown_backend_prefix_is_a_caller_mistake(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "nope__thing", "arguments": {}})
        assert response["error"]["code"] == errors.INVALID_PARAMS
        assert set(response["error"]["data"]["knownBackends"]) == {"alpha", "beta"}
        assert "source" not in response["error"]["data"]
    finally:
        await harness.close()


async def test_an_unknown_tool_on_a_real_backend_is_the_backends_answer(tmp_path) -> None:
    """We do not consult a cache to decide this: the backend says whether it has the tool."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "alpha__nosuch", "arguments": {}})
        assert response["error"]["data"]["source"] == errors.BACKEND_SOURCE
    finally:
        await harness.close()


async def test_a_name_with_no_separator_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "bare", "arguments": {}})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


async def test_arguments_are_forwarded_unchanged(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert await client.tool_text("alpha__echo", {"text": "a b\tc"}) == "alpha:echo:a b\tc"
    finally:
        await harness.close()


async def test_a_call_updates_last_call_at(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert harness.gateway.backend("alpha").last_call_at is None
        await client.tool_text("alpha__echo", {})
        assert harness.gateway.backend("alpha").last_call_at is not None
    finally:
        await harness.close()
