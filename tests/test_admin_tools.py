"""The gateway's own meta-tools, and the promise that they never carry a credential."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_gateway import errors
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
SECRET = "ghp_a_real_looking_token"

SERVERS = f"""
servers:
  alpha:
    command: {sys.executable}
    args: ["{FIXTURE}"]
    env:
      MY_TOKEN: "${{ALPHA_TOKEN}}"
    description: "The alpha backend"
  beta:
    command: /usr/bin/true
    enabled: false
  gamma:
    command: /usr/bin/true
    env:
      G: "${{NEVER_SET}}"
"""
ENV = f"ALPHA_TOKEN={SECRET}\n"


async def admin_json(client, tool: str, arguments: dict | None = None):
    result = await client.call(
        "tools/call", {"name": f"gateway__{tool}", "arguments": arguments or {}}
    )
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def test_the_meta_tools_are_always_listed(tmp_path) -> None:
    """Even with zero backends -- which is why `tools` is advertised unconditionally."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert {
            "gateway__list_backends",
            "gateway__backend_health",
            "gateway__restart_backend",
            "gateway__reload_config",
        } <= set(names)
    finally:
        await harness.close()


async def test_every_meta_tool_has_a_closed_schema(tmp_path) -> None:
    """`additionalProperties: false` so a model's invented argument fails loudly."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        tools = (await client.call("tools/list"))["tools"]
        for tool in tools:
            assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]
            assert tool["description"], tool["name"]
    finally:
        await harness.close()


async def test_list_backends_reports_every_configured_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        backends = await admin_json(client, "list_backends")
        by_name = {b["name"]: b for b in backends}
        assert set(by_name) == {"alpha", "beta", "gamma"}
        assert by_name["beta"]["enabled"] is False
        assert by_name["beta"]["status"] == "disabled"
        assert by_name["alpha"]["description"] == "The alpha backend"
    finally:
        await harness.close()


async def test_list_backends_reports_key_names_and_never_values(tmp_path) -> None:
    """The promise that makes the meta-tools safe to hand a model."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        result = await client.call(
            "tools/call", {"name": "gateway__list_backends", "arguments": {}}
        )
        payload = result["content"][0]["text"]
        assert "MY_TOKEN" in payload
        assert SECRET not in payload
        assert "ALPHA_TOKEN" not in payload  # not even the store-side key name
    finally:
        await harness.close()


async def test_backend_health_reports_one_backend_by_name(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        health = await admin_json(client, "backend_health", {"name": "alpha"})
        assert health["name"] == "alpha"
        assert "uptime_seconds" in health and "restart_count" in health
        assert "ping_ms" in health
    finally:
        await harness.close()


async def test_backend_health_never_leaks_a_value_either(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        result = await client.call("tools/call", {"name": "gateway__backend_health", "arguments": {}})
        assert SECRET not in result["content"][0]["text"]
    finally:
        await harness.close()


async def test_an_unknown_backend_is_a_tool_level_error_not_a_protocol_one(tmp_path) -> None:
    """So a model can read the answer and say which backends *do* exist."""
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        result = await client.call(
            "tools/call", {"name": "gateway__backend_health", "arguments": {"name": "nope"}}
        )
        assert result["isError"] is True
        assert "nope" in result["content"][0]["text"]
    finally:
        await harness.close()


async def test_an_unknown_gateway_tool_is_invalid_params(tmp_path) -> None:
    """A caller mistake, not a runtime condition -- so a protocol error is right here."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        response = await client.request(
            "tools/call", {"name": "gateway__no_such_tool", "arguments": {}}
        )
        assert response["error"]["code"] == errors.INVALID_PARAMS
        assert "knownTools" in response["error"]["data"]
    finally:
        await harness.close()


async def test_calling_an_unknown_backend_tool_names_the_backends_that_exist(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS, env=ENV)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"name": "nosuch__tool", "arguments": {}})
        assert response["error"]["code"] == errors.INVALID_PARAMS
        assert "alpha" in response["error"]["data"]["knownBackends"]
        # Ours, not a backend's: the discriminator has to stay absent.
        assert "source" not in response["error"]["data"]
    finally:
        await harness.close()


async def test_tools_call_without_a_name_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        response = await client.request("tools/call", {"arguments": {}})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


async def test_a_backend_cannot_shadow_the_meta_tools(tmp_path) -> None:
    """`gateway` is reserved at config load, which is the only place a name is chosen."""
    from mcp_gateway.config import ConfigError, parse

    try:
        parse("servers:\n  gateway:\n    command: x\n")
        raise AssertionError("a backend named 'gateway' should be refused")
    except ConfigError as exc:
        assert "reserved" in str(exc)
