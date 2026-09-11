"""Several clients, one daemon, one backend pool."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp_gateway.protocol import MCP_PROTOCOL_VERSION
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  a:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "a"
      MOCK_TOOLS: "echo"
  b:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "b"
      MOCK_TOOLS: "echo"
"""


async def test_every_client_sees_the_same_backend_processes(tmp_path) -> None:
    """One npx cold start total. This is what makes it a daemon rather than a launcher."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        one = await harness.connect()
        two = await harness.connect()
        assert await one.tool_names() == await two.tool_names()

        pids_before = {b.name: b.pid for b in harness.gateway.backends()}
        three = await harness.connect()
        await three.tool_names()
        assert {b.name: b.pid for b in harness.gateway.backends()} == pids_before
    finally:
        await harness.close()


async def test_each_connection_keeps_its_own_negotiated_version(tmp_path) -> None:
    """The reason a Session is per-connection: a shared one answers with the wrong gates."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        old = await harness.connect(initialize=False)
        new = await harness.connect(initialize=False)
        assert (await old.initialize(version="2024-11-05"))["protocolVersion"] == "2024-11-05"
        assert (await new.initialize())["protocolVersion"] == MCP_PROTOCOL_VERSION
    finally:
        await harness.close()


async def test_each_connection_keeps_its_own_declared_capabilities(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        await harness.connect(capabilities={"roots": {"listChanged": False}})
        await harness.connect()
        declared = [s.client_capabilities for s in harness.gateway._sessions]
        assert {"roots"} in [set(d) for d in declared]
        assert set() in [set(d) for d in declared]
        # The union is what backends are told, and it is the union, not either one.
        assert harness.gateway.client_capability_union().roots is True
    finally:
        await harness.close()


async def test_the_union_shrinks_when_a_client_leaves(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect(capabilities={"roots": {"listChanged": False}})
        assert harness.gateway.client_capability_union().roots is True
        await client.close()
        for _ in range(100):
            if not harness.gateway._sessions:
                break
            await asyncio.sleep(0.02)
        assert harness.gateway.client_capability_union().roots is False
    finally:
        await harness.close()


async def test_concurrent_calls_from_two_clients_both_complete(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        one = await harness.connect()
        two = await harness.connect()
        results = await asyncio.gather(
            one.tool_text("a__echo", {"text": "from-one"}),
            two.tool_text("b__echo", {"text": "from-two"}),
        )
        assert results == ["a:echo:from-one", "b:echo:from-two"]
    finally:
        await harness.close()


async def test_one_client_disconnecting_leaves_the_others_working(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        doomed = await harness.connect()
        survivor = await harness.connect()
        await doomed.close()
        await asyncio.sleep(0.1)
        assert await survivor.tool_text("a__echo", {"text": "still here"}) == "a:echo:still here"
        assert len(harness.gateway.backends()) == 2
    finally:
        await harness.close()


async def test_admin_status_describes_every_connection(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        await harness.connect()
        await harness.connect()
        status = await harness.gateway.admin.status({})
        assert len(status["connections"]) == 2
        assert all(c["path"] == "/mcp" for c in status["connections"])
        assert all(c["initialized"] for c in status["connections"])
    finally:
        await harness.close()


async def test_a_restart_is_seen_by_every_client(tmp_path) -> None:
    """Shared state means shared consequences, which is the point."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        one = await harness.connect()
        two = await harness.connect()
        before = harness.gateway.backend("a").pid

        await one.call("tools/call", {"name": "gateway__restart_backend", "arguments": {"name": "a"}})
        after = harness.gateway.backend("a").pid
        assert after != before

        health = json.loads(
            (
                await two.call(
                    "tools/call", {"name": "gateway__backend_health", "arguments": {"name": "a"}}
                )
            )["content"][0]["text"]
        )
        assert health["pid"] == after
    finally:
        await harness.close()
