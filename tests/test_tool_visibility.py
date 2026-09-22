"""Which of a backend's tools `/mcp` advertises, and everything that must not change with it.

The invariant the rest of these tests exist to protect is the second one: a hidden tool is
still **callable**. Hiding is context economy, not access control, and the moment `tools/call`
starts refusing a hidden name this feature has quietly become a permission system nobody
designed. See `src/mcp_gateway/visibility.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_gateway.visibility import BENCH_CLIENT
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

BENCH = {"name": BENCH_CLIENT, "version": "0"}


def servers(**entries: dict) -> str:
    lines = ["servers:"]
    for name, env in entries.items():
        lines.append(f"  {name}:")
        lines.append(f"    command: {PY}")
        lines.append(f'    args: ["{FIXTURE}"]')
        if env:
            lines.append("    env:")
            for key, value in env.items():
                lines.append(f'      {key}: "{value}"')
    return "\n".join(lines) + "\n"


ONE_BACKEND = servers(alpha={"MOCK_TOOLS": "echo,other"})


async def test_everything_is_advertised_until_somebody_hides_something(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        names = await (await harness.connect()).tool_names()
        assert "alpha__echo" in names
        assert "alpha__other" in names
    finally:
        await harness.close()


async def test_a_hidden_tool_leaves_the_listing_and_nothing_else_does(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})

        names = await (await harness.connect()).tool_names()
        assert "alpha__echo" not in names
        assert "alpha__other" in names
        # The meta-tools are never filtered: they are the model's map of the gateway.
        assert "gateway__list_backends" in names
    finally:
        await harness.close()


async def test_a_hidden_tool_is_still_callable(tmp_path) -> None:
    """The load-bearing invariant. Routing is a pure string split with no lookup table, and
    a listing is not a permission. Anything that makes this fail has changed what the
    feature is."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})

        client = await harness.connect()
        assert "alpha__echo" not in await client.tool_names()
        result = await client.call("tools/call", {"name": "alpha__echo", "arguments": {}})
        assert not result.get("isError"), result
    finally:
        await harness.close()


async def test_the_bench_sees_a_hidden_tool_and_an_ordinary_client_does_not(tmp_path) -> None:
    """The admin UI's own `/mcp` session is exempt, on the same daemon, at the same moment.

    Without this there is no way back: the page builds its checkbox list from `tools/list`,
    so a filtered bench could only ever offer the tools that are already visible.
    """
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})

        bench = await harness.connect(client_info=BENCH)
        ordinary = await harness.connect()
        assert "alpha__echo" in await bench.tool_names()
        assert "alpha__echo" not in await ordinary.tool_names()
    finally:
        await harness.close()


async def test_admin_backends_carries_the_selection(tmp_path) -> None:
    """Read back through the payload the UI already fetches, not a method of its own."""
    harness = await daemon(tmp_path, servers=servers(alpha={"MOCK_TOOLS": "echo"}, beta={"MOCK_TOOLS": "echo"}))
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})

        by_name = {b["name"]: b for b in (await admin.call("admin.backends"))["backends"]}
        assert by_name["alpha"]["hidden_tools"] == ["echo"]
        assert by_name["beta"]["hidden_tools"] == []

        health = await admin.call("admin.health", {"name": "alpha"})
        assert health["hidden_tools"] == ["echo"]
    finally:
        await harness.close()


async def test_the_set_is_replaced_rather_than_merged(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})
        result = await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["other"]})
        assert result == {"name": "alpha", "hidden": ["other"], "changed": True}

        names = await (await harness.connect()).tool_names()
        assert "alpha__echo" in names
        assert "alpha__other" not in names
    finally:
        await harness.close()


async def test_an_identical_set_reports_no_change(tmp_path) -> None:
    """What the caller guards the `list_changed` broadcast on."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})
        again = await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})
        assert again["changed"] is False
    finally:
        await harness.close()


async def test_selecting_none_and_then_all(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo", "other"]})
        assert [n for n in await (await harness.connect()).tool_names() if n.startswith("alpha__")] == []

        await admin.call("admin.tools.hide", {"name": "alpha", "tools": []})
        names = await (await harness.connect()).tool_names()
        assert "alpha__echo" in names
        assert "alpha__other" in names
    finally:
        await harness.close()


@pytest.mark.parametrize("name", ["nope", "gateway"])
async def test_a_backend_that_is_not_one_is_refused(tmp_path, name) -> None:
    """`gateway` included: the meta-tools are not selectable, so the pseudo-entry the header
    draws for them must not be addressable here either."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        response = await admin.request("admin.tools.hide", {"name": name, "tools": []})
        assert response["error"]["code"] == -32602
    finally:
        await harness.close()


async def test_tools_must_be_a_list_of_names(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        response = await admin.request("admin.tools.hide", {"name": "alpha", "tools": "echo"})
        assert response["error"]["code"] == -32602
    finally:
        await harness.close()


async def test_a_name_the_backend_does_not_publish_is_accepted(tmp_path) -> None:
    """Refusing would break the control exactly when the listing is stale -- a backend that
    is down publishes nothing, and that is when somebody is most likely to be pruning."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        result = await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["ghost"]})
        assert result["hidden"] == ["ghost"]

        names = await (await harness.connect()).tool_names()
        assert "alpha__echo" in names
        assert "alpha__other" in names
    finally:
        await harness.close()


async def test_a_restart_does_not_un_hide(tmp_path) -> None:
    """A restart is the same backend. A selection that did not survive one would be a
    selection nobody could rely on."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})
        await admin.call("admin.backend.restart", {"name": "alpha"})

        assert "alpha__echo" not in await (await harness.connect()).tool_names()
    finally:
        await harness.close()


async def test_a_reload_that_removes_the_backend_forgets_its_selection(tmp_path) -> None:
    """The one case that prunes: there is nothing left for those names to name."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})

        (tmp_path / "servers.yaml").write_text(servers(beta={"MOCK_TOOLS": "echo"}))
        await admin.call("admin.reload")
        (tmp_path / "servers.yaml").write_text(ONE_BACKEND)
        await admin.call("admin.reload")

        assert "alpha__echo" in await (await harness.connect()).tool_names()
    finally:
        await harness.close()


async def test_hiding_does_not_change_what_health_reports(tmp_path) -> None:
    """The reason the filter is in `gateway.list_tools` and not in the catalogue: a backend
    must not look to `gateway__backend_health` like it publishes less than it does."""
    harness = await daemon(tmp_path, servers=ONE_BACKEND)
    try:
        admin = await harness.connect("/admin", initialize=False)
        # A listing first, so `tool_count` is a number rather than "nobody has asked".
        await (await harness.connect()).tool_names()
        before = await admin.call("admin.health", {"name": "alpha"})
        assert before["tool_count"] == 2

        await admin.call("admin.tools.hide", {"name": "alpha", "tools": ["echo"]})
        after = await admin.call("admin.health", {"name": "alpha"})
        assert after["tool_count"] == 2
        assert after["skipped_tools"] == before["skipped_tools"]
    finally:
        await harness.close()


def test_hiding_is_not_a_verb_the_model_can_reach() -> None:
    """`admin_channel.md`: admin verbs must not appear in the model's tool list. A model
    that could hide tools could hide `gateway__backend_health` from the next session."""
    from mcp_gateway.admin import tool_definitions

    names = [tool["name"] for tool in tool_definitions(["alpha"])]
    assert not [name for name in names if "hide" in name or "visib" in name]
