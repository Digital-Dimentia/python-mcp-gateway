"""Prompts fan out the same way tools do, with the same namespacing."""

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
      MOCK_PROMPTS: "greet,farewell"
  beta:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "beta"
      MOCK_PROMPTS: "greet"
  toolsonly:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_CAPABILITIES: "tools"
"""


async def test_prompts_are_namespaced_across_backends(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        names = [p["name"] for p in (await client.call("prompts/list"))["prompts"]]
        assert "alpha__greet" in names
        assert "alpha__farewell" in names
        assert "beta__greet" in names
    finally:
        await harness.close()


async def test_a_backend_that_declares_no_prompts_contributes_none(tmp_path) -> None:
    """The capability block is consulted, so we never ask a server that said no."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        names = [p["name"] for p in (await client.call("prompts/list"))["prompts"]]
        assert not any(n.startswith("toolsonly__") for n in names)
    finally:
        await harness.close()


async def test_getting_a_prompt_reaches_the_right_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        result = await client.call("prompts/get", {"name": "alpha__greet"})
        assert "alpha says greet" in result["messages"][0]["content"]["text"]
        other = await client.call("prompts/get", {"name": "beta__greet"})
        assert "beta says greet" in other["messages"][0]["content"]["text"]
    finally:
        await harness.close()


async def test_arguments_are_forwarded(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        result = await client.call(
            "prompts/get", {"name": "alpha__greet", "arguments": {"who": "world"}}
        )
        assert result["description"]
    finally:
        await harness.close()


async def test_an_unknown_backend_prefix_is_invalid_params(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("prompts/get", {"name": "nope__greet"})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


async def test_an_unknown_prompt_on_a_real_backend_is_the_backends_answer(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("prompts/get", {"name": "alpha__nosuch"})
        assert response["error"]["data"]["source"] == errors.BACKEND_SOURCE
        assert response["error"]["data"]["backend"] == "alpha"
    finally:
        await harness.close()


async def test_prompts_get_without_a_name_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("prompts/get", {})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()
