"""Cancellation composes end to end -- but only if nobody swallows `CancelledError`.

A client cancels; the session cancels its serving task; `MCPStdioClient.request` catches the
cancellation and sends `notifications/cancelled` down the backend's own wire. Nothing in the
middle has to know that, which is exactly why it needs a test: it is free only for as long
as every layer keeps re-raising.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

STALLS = f"""
servers:
  a:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "a"
      MOCK_TOOLS: "quick"
      MOCK_STALL_ON_CALL: "stall"
"""


async def cancelled_ids(client) -> list[int]:
    """Ask the backend which request ids it was told to forget."""
    return json.loads(await client.tool_text("a__cancel-report", {}))


async def test_a_client_cancel_reaches_the_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=STALLS)
    try:
        client = await harness.connect()
        assert await cancelled_ids(client) == []

        # Fire a call that will never be answered, then cancel it.
        client._id += 1
        request_id = client._id
        future = asyncio.get_running_loop().create_future()
        client._pending[request_id] = future
        await client._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "a__stall", "arguments": {}},
            }
        )
        await asyncio.sleep(0.3)
        await client.notify("notifications/cancelled", {"requestId": request_id})

        for _ in range(100):
            if await cancelled_ids(client):
                break
            await asyncio.sleep(0.05)
        assert await cancelled_ids(client), "the backend should have been told to stop"
    finally:
        await harness.close()


async def test_a_disconnect_cancels_that_connections_work(tmp_path) -> None:
    """A client that hung up must not leave a backend working on a reply nobody will read."""
    harness = await daemon(tmp_path, servers=STALLS)
    try:
        doomed = await harness.connect()
        await doomed._send(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "a__stall", "arguments": {}},
            }
        )
        await asyncio.sleep(0.3)
        await doomed.close()

        observer = await harness.connect()
        for _ in range(100):
            if await cancelled_ids(observer):
                break
            await asyncio.sleep(0.05)
        assert await cancelled_ids(observer), "the disconnect should have propagated"
    finally:
        await harness.close()


async def test_one_clients_cancel_does_not_disturb_another(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=STALLS)
    try:
        one = await harness.connect()
        two = await harness.connect()

        pending = asyncio.create_task(two.call("tools/call", {"name": "a__quick", "arguments": {"text": "ok"}}))
        await one.notify("notifications/cancelled", {"requestId": 12345})
        result = await asyncio.wait_for(pending, 10)
        assert result["content"][0]["text"] == "a:quick:ok"
    finally:
        await harness.close()


async def test_cancelling_an_unknown_request_is_harmless(tmp_path) -> None:
    """A cancel that races the response is ordinary, not an error."""
    harness = await daemon(tmp_path, servers=STALLS)
    try:
        client = await harness.connect()
        await client.notify("notifications/cancelled", {"requestId": 4242})
        assert await client.call("ping") == {}
    finally:
        await harness.close()
