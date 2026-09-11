"""The eager sweep: concurrency, skipping, readiness, and the `required` refusal."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from mcp_gateway.config import parse as parse_config
from mcp_gateway.secrets import SecretStore
from mcp_gateway.supervisor import RequiredBackendFailed, Supervisor
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable


def entry(name: str, **env: str) -> str:
    lines = [f"  {name}:", f"    command: {PY}", f'    args: ["{FIXTURE}"]']
    if env:
        lines.append("    env:")
        lines += [f'      {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines) + "\n"


async def supervisor_for(config_text: str, store: SecretStore | None = None) -> Supervisor:
    return Supervisor(parse_config(config_text), store or SecretStore())


async def test_backends_start_concurrently_not_one_after_another(tmp_path) -> None:
    """Twelve backends that each take two seconds should cost two seconds, not twenty-four."""
    slow = "servers:\n" + "".join(
        entry(f"s{i}", MOCK_SLOW_START_MS="400") for i in range(4)
    )
    supervisor = await supervisor_for(slow)
    started = time.monotonic()
    try:
        await supervisor.start_all()
        elapsed = time.monotonic() - started
        assert len(supervisor.running) == 4
        # Four 400 ms starts: well under the 1.6 s a serial sweep would take.
        assert elapsed < 1.2, elapsed
    finally:
        await supervisor.close_all()


async def test_one_failing_backend_does_not_stop_the_others() -> None:
    config = "servers:\n" + entry("good") + "  bad:\n    command: /nonexistent/binary\n"
    supervisor = await supervisor_for(config)
    try:
        await supervisor.start_all()
        assert [b.name for b in supervisor.running] == ["good"]
        assert supervisor.get("bad").status.value == "failed"
    finally:
        await supervisor.close_all()


async def test_a_required_backend_that_fails_refuses_the_startup() -> None:
    """The opt-in for a deployment where silent degradation is unacceptable."""
    config = "servers:\n  must:\n    command: /nonexistent/binary\n    required: true\n"
    supervisor = await supervisor_for(config)
    try:
        with pytest.raises(RequiredBackendFailed) as caught:
            await supervisor.start_all()
        assert caught.value.name == "must"
    finally:
        await supervisor.close_all()


async def test_a_backend_exists_for_every_entry_including_disabled_ones() -> None:
    """So `gateway__list_backends` can say *why* each one is not serving."""
    config = "servers:\n" + entry("live") + "  parked:\n    command: /usr/bin/true\n    enabled: false\n"
    supervisor = await supervisor_for(config)
    try:
        await supervisor.start_all()
        assert {b.name for b in supervisor.all} == {"live", "parked"}
        assert supervisor.get("parked").status.value == "disabled"
    finally:
        await supervisor.close_all()


async def test_readiness_is_signalled_after_the_sweep() -> None:
    supervisor = await supervisor_for("servers:\n" + entry("a"))
    try:
        assert not supervisor.ready.is_set()
        await supervisor.start_all()
        assert supervisor.ready.is_set()
        assert await supervisor.wait_ready(ceiling=0.1) is True
    finally:
        await supervisor.close_all()


async def test_waiting_past_the_ceiling_answers_rather_than_blocking_forever() -> None:
    """A client blocked forever is worse than one told less than the whole truth."""
    supervisor = await supervisor_for("servers: {}\n")
    assert await supervisor.wait_ready(ceiling=0.05) is False


async def test_a_listing_waits_for_the_sweep_rather_than_returning_empty(tmp_path) -> None:
    """An empty tools/list is indistinguishable to a model from "nothing here"."""
    config = "servers:\n" + entry("slow", MOCK_SLOW_START_MS="300", MOCK_TOOLS="present")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        assert "slow__present" in await client.tool_names()
    finally:
        await harness.close()


async def test_backends_are_up_before_the_socket_binds(tmp_path) -> None:
    """So the first client finds a warm pool, and /admin can answer with no client attached."""
    config = "servers:\n" + entry("a", MOCK_TOOLS="ready")
    harness = await daemon(tmp_path, servers=config)
    try:
        assert harness.gateway.supervisor.ready.is_set()
        assert [b.name for b in harness.gateway.supervisor.running] == ["a"]
    finally:
        await harness.close()


async def test_close_all_reaps_every_backend() -> None:
    supervisor = await supervisor_for("servers:\n" + entry("a") + entry("b"))
    await supervisor.start_all()
    procs = [b.client._proc for b in supervisor.running]
    await supervisor.close_all()
    assert all(proc.returncode is not None for proc in procs)


async def test_close_all_survives_a_backend_that_will_not_die() -> None:
    """One that will not die must not prevent the other from being reaped."""
    config = "servers:\n" + entry("stubborn", MOCK_IGNORE_EOF="1", MOCK_IGNORE_SIGTERM="1") + entry("normal")
    supervisor = await supervisor_for(config)
    await supervisor.start_all()
    procs = [b.client._proc for b in supervisor.running]
    await asyncio.wait_for(supervisor.close_all(), 20)
    assert all(proc.returncode is not None for proc in procs)


async def test_a_restart_recomputes_the_capability_block() -> None:
    """A restart is the only moment a backend can be told something new.

    MCP has no renegotiation, so a capability declared by a client that arrived later
    reaches only backends started after it. An `on_server_request` handler is required
    alongside: `MCPStdioClient` refuses to declare a capability with nothing behind it.
    """
    from mcp_gateway.mcp_stdio import MCPClientCapabilities

    declared = MCPClientCapabilities()

    def current() -> MCPClientCapabilities:
        return declared

    async def answer(_name: str, _method: str, _params: dict) -> dict:
        return {"roots": []}

    supervisor = Supervisor(
        parse_config("servers:\n" + entry("a")),
        SecretStore(),
        client_capabilities=current,
        on_server_request=answer,
    )
    try:
        await supervisor.start_all()
        assert supervisor.get("a").client_capabilities.roots is False
        declared = MCPClientCapabilities(roots=True)
        await supervisor.restart("a")
        assert supervisor.get("a").client_capabilities.roots is True
    finally:
        await supervisor.close_all()


async def test_the_capability_union_follows_the_attached_clients(tmp_path) -> None:
    """MCP has no renegotiation, so a backend keeps whatever it was told at its own start.

    A client declaring `roots` after the sweep therefore reaches only backends started
    afterwards -- which is precisely why `backend_request` has to be able to refuse, rather
    than assuming an accepted request can always be placed.
    """
    config = "servers:\n" + entry("a")
    harness = await daemon(tmp_path, servers=config)
    try:
        # Nobody attached during the sweep, so the backend was told nothing.
        assert harness.gateway.supervisor.get("a").client_capabilities.roots is False

        await harness.connect(capabilities={"roots": {"listChanged": False}})
        assert harness.gateway.client_capability_union().roots is True

        # The already-running backend still carries the old promise...
        assert harness.gateway.supervisor.get("a").client_capabilities.roots is False
        # ...and a restart is the one moment it can be told otherwise.
        await harness.gateway.restart_backend("a")
        assert harness.gateway.supervisor.get("a").client_capabilities.roots is True
    finally:
        await harness.close()
