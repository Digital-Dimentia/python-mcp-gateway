"""Reload: rotate a credential, add a server, disable another -- and disturb nothing else.

The property under test is not "reload works". It is that an **unchanged backend keeps its
process**. In a daemon with agents mid-turn, reloading to add one server must not drop
in-flight work on the other eleven, and the only thing that makes that true is comparing
*resolved* spawn identity rather than restarting everything.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable


def entry(name: str, *, enabled: bool = True, **env: str) -> str:
    lines = [f"  {name}:", f"    command: {PY}", f'    args: ["{FIXTURE}"]']
    if not enabled:
        lines.append("    enabled: false")
    if env:
        lines.append("    env:")
        lines += [f'      {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines) + "\n"


THREE = (
    "servers:\n"
    + entry("alpha", MOCK_NAME="alpha", MOCK_ENV_REPORT="1", TOKEN="${ALPHA_TOKEN}")
    + entry("beta", MOCK_NAME="beta")
    + entry("gamma", MOCK_NAME="gamma")
)
ENV = "ALPHA_TOKEN=original-token-value\n"


async def reload_via_tool(client) -> dict:
    result = await client.call(
        "tools/call", {"name": "gateway__reload_config", "arguments": {}}
    )
    assert not result.get("isError"), result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def pids(harness) -> dict:
    return {b.name: b.pid for b in harness.gateway.backends()}


async def test_rotating_a_credential_restarts_only_the_backend_that_uses_it(tmp_path) -> None:
    """The headline. Comparing the ${VAR} templates would see no change here at all."""
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)

        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated-token-value\n")
        plan = await reload_via_tool(client)

        assert plan["restarted"] == ["alpha"]
        assert sorted(plan["unchanged"]) == ["beta", "gamma"]

        after = pids(harness)
        assert after["alpha"] != before["alpha"], "the rotated backend must be replaced"
        assert after["beta"] == before["beta"], "an unchanged backend must keep its process"
        assert after["gamma"] == before["gamma"]
    finally:
        await harness.close()


async def test_the_new_credential_actually_reaches_the_child(tmp_path) -> None:
    """Restarting is not the point; restarting *with the new value* is."""
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated-token-value\n")
        await reload_via_tool(client)

        names = json.loads(await client.tool_text("alpha__env-report", {}))
        assert "TOKEN" in names
        backend = harness.gateway.backend("alpha")
        assert backend.running
    finally:
        await harness.close()


async def test_adding_and_disabling_servers(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)

        updated = (
            "servers:\n"
            + entry("alpha", MOCK_NAME="alpha", MOCK_ENV_REPORT="1", TOKEN="${ALPHA_TOKEN}")
            + entry("beta", MOCK_NAME="beta")
            + entry("gamma", MOCK_NAME="gamma", enabled=False)
            + entry("delta", MOCK_NAME="delta")
        )
        (tmp_path / "servers.yaml").write_text(updated)
        plan = await reload_via_tool(client)

        assert plan["added"] == ["delta"]
        assert "gamma" in plan["restarted"], "disabling changes the spawn identity"
        assert harness.gateway.backend("delta").running
        assert harness.gateway.backend("gamma").status.value == "disabled"
        assert pids(harness)["alpha"] == before["alpha"]
        assert "delta__echo" in await client.tool_names()
        assert not any(n.startswith("gamma__") for n in await client.tool_names())
    finally:
        await harness.close()


async def test_removing_a_server_stops_it(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        proc = harness.gateway.backend("gamma").client._proc

        (tmp_path / "servers.yaml").write_text(
            "servers:\n"
            + entry("alpha", MOCK_NAME="alpha", MOCK_ENV_REPORT="1", TOKEN="${ALPHA_TOKEN}")
            + entry("beta", MOCK_NAME="beta")
        )
        plan = await reload_via_tool(client)

        assert plan["removed"] == ["gamma"]
        assert harness.gateway.backend("gamma") is None
        assert proc.returncode is not None, "a removed backend must be reaped"
    finally:
        await harness.close()


async def test_exactly_one_list_changed_reaches_the_client(tmp_path) -> None:
    """Eight restarted backends must not cost a client eight fan-out refetches."""
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        client.notifications.clear()

        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated\n")
        await reload_via_tool(client)
        await asyncio.sleep(0.5)

        tools_changed = [
            n for n in client.notifications if n["method"] == "notifications/tools/list_changed"
        ]
        assert len(tools_changed) == 1, tools_changed
    finally:
        await harness.close()


async def test_a_broken_config_changes_nothing_at_all(tmp_path) -> None:
    """Load-then-replace: there is never a moment when half a catalogue has been applied."""
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)

        (tmp_path / "servers.yaml").write_text("servers:\n  alpha:\n    typo: yes\n")
        result = await client.call(
            "tools/call", {"name": "gateway__reload_config", "arguments": {}}
        )

        assert result["isError"] is True
        assert "nothing changed" in result["content"][0]["text"]
        assert pids(harness) == before
        assert await client.tool_names()
    finally:
        await harness.close()


async def test_a_broken_credential_store_changes_nothing_either(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)

        (tmp_path / "gateway.env").write_text("TOKEN=a\nTOKEN=b\n")
        result = await client.call(
            "tools/call", {"name": "gateway__reload_config", "arguments": {}}
        )
        assert result["isError"] is True
        assert pids(harness) == before
    finally:
        await harness.close()


async def test_dry_run_reports_the_plan_and_touches_nothing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)

        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated\n")
        result = await client.call(
            "tools/call", {"name": "gateway__reload_config", "arguments": {"dry_run": True}}
        )
        plan = json.loads(result["content"][0]["text"])

        assert plan["dry_run"] is True
        assert plan["restarted"] == ["alpha"]
        assert pids(harness) == before, "dry_run must touch nothing"
    finally:
        await harness.close()


async def test_a_reload_with_no_changes_restarts_nothing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        before = pids(harness)
        plan = await reload_via_tool(client)
        assert sorted(plan["unchanged"]) == ["alpha", "beta", "gamma"]
        assert plan["restarted"] == []
        assert pids(harness) == before
    finally:
        await harness.close()


async def test_the_connection_survives_a_reload(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated\n")
        await reload_via_tool(client)
        assert await client.call("ping") == {}
        assert await client.tool_text("beta__echo", {"text": "still here"}) == "beta:echo:still here"
    finally:
        await harness.close()


async def test_a_server_that_fails_to_start_is_reported_not_raised(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        (tmp_path / "servers.yaml").write_text(
            "servers:\n"
            + entry("beta", MOCK_NAME="beta")
            + "  broken:\n    command: /nonexistent/binary\n"
        )
        plan = await reload_via_tool(client)
        assert [f["name"] for f in plan["failed"]] == ["broken"]
        assert harness.gateway.backend("beta").running
    finally:
        await harness.close()


async def test_the_plan_never_contains_a_credential_or_a_digest(tmp_path) -> None:
    """Only 'changed' or 'unchanged' is ever reported: a digest of a low-entropy secret
    is a secret."""
    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=rotated-token-value\n")
        result = await client.call(
            "tools/call", {"name": "gateway__reload_config", "arguments": {}}
        )
        payload = result["content"][0]["text"]
        assert "rotated-token-value" not in payload
        assert "original-token-value" not in payload
        assert "sha256" not in payload.lower()
        assert len([w for w in payload.split() if len(w) >= 64]) == 0
    finally:
        await harness.close()


async def test_redaction_follows_the_rotation(tmp_path) -> None:
    """The old value stops being scrubbed; the new one starts."""
    import logging

    harness = await daemon(tmp_path, servers=THREE, env=ENV)
    try:
        client = await harness.connect()
        (tmp_path / "gateway.env").write_text("ALPHA_TOKEN=brand-new-token-value\n")
        await reload_via_tool(client)

        records: list[str] = []
        handler = logging.StreamHandler()
        handler.emit = lambda record: records.append(handler.format(record))  # type: ignore[method-assign]
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            logging.getLogger("probe").warning(
                "old=%s new=%s", "original-token-value", "brand-new-token-value"
            )
        finally:
            root.removeHandler(handler)

        line = records[-1]
        assert "brand-new-token-value" not in line, "the new value must be scrubbed"
        assert "original-token-value" in line, "the old value is no longer a secret"
    finally:
        await harness.close()
