"""A panel reference, from a real backend subprocess to a real client over the socket.

`tests/test_ui_apps.py` covers the rewrite as a function. This file is about the two things
only a running gateway can show: that the address a client is handed actually resolves, and
that it resolves to the backend that *published the tool* rather than the one the `ui://`
authority names.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_gateway import naming
from tests.fixtures.ws_client import daemon

PY = sys.executable
FIXTURE = str(Path(__file__).parent / "fixtures" / "mock_backend.py")


def entry(name: str, **env: str) -> str:
    lines = [f"  {name}:", f"    command: {PY}", f'    args: ["{FIXTURE}"]']
    if env:
        lines.append("    env:")
        lines += [f'      {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines) + "\n"


def tool_named(tools: list[dict], name: str) -> dict:
    return next(tool for tool in tools if tool["name"] == name)


def reference_in(tool: dict) -> str:
    return tool["_meta"]["ui"]["resourceUri"]


async def test_a_tool_reaches_a_client_with_a_resolvable_panel_uri(tmp_path) -> None:
    servers = "servers:\n" + entry(
        "zoo",
        MOCK_TOOLS="board",
        MOCK_UI_TOOL="board",
        MOCK_UI_REF="ui://zoo/panel",
        MOCK_RESOURCES="ui://zoo/panel",
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        tools = (await client.call("tools/list"))["tools"]
        reference = reference_in(tool_named(tools, "zoo__board"))

        # Not the backend's own spelling: a client that asked for that would get -32602.
        assert reference.startswith("mcpgw://zoo/")
        assert naming.decode_resource_uri(reference) == ("zoo", "ui://zoo/panel")

        read = await client.call("resources/read", {"uri": reference})
        assert read["contents"], "the address the client was handed must actually resolve"
    finally:
        await harness.close()


async def test_a_panel_reference_to_another_backends_uri_reads_from_the_referrer(tmp_path) -> None:
    """The security property, end to end.

    `evil` publishes a tool whose reference names `ui://zoo/panel`, and `zoo` really does
    publish that resource. The reference must still be addressed to *evil* -- so the read
    reaches evil, which has no such resource, and fails. Were the authority resolved against
    the server named `zoo`, this would succeed and hand zoo's panel over under evil's name.
    """
    servers = (
        "servers:\n"
        + entry("zoo", MOCK_TOOLS="board", MOCK_RESOURCES="ui://zoo/panel")
        + entry("evil", MOCK_TOOLS="board", MOCK_UI_TOOL="board", MOCK_UI_REF="ui://zoo/panel")
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        tools = (await client.call("tools/list"))["tools"]
        reference = reference_in(tool_named(tools, "evil__board"))

        assert naming.decode_resource_uri(reference) == ("evil", "ui://zoo/panel")

        read = await client.request("resources/read", {"uri": reference})
        assert "error" in read, "evil must not be able to serve zoo's panel"
    finally:
        await harness.close()


async def test_a_tool_without_a_panel_is_untouched(tmp_path) -> None:
    servers = "servers:\n" + entry("zoo", MOCK_TOOLS="board")
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        tools = (await client.call("tools/list"))["tools"]
        assert "_meta" not in tool_named(tools, "zoo__board")
    finally:
        await harness.close()


async def test_the_deprecated_flat_spelling_is_rewritten_end_to_end(tmp_path) -> None:
    servers = "servers:\n" + entry(
        "zoo",
        MOCK_TOOLS="board",
        MOCK_UI_TOOL="board",
        MOCK_UI_FLAT="1",
        MOCK_UI_REF="ui://zoo/panel",
        MOCK_RESOURCES="ui://zoo/panel",
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        tools = (await client.call("tools/list"))["tools"]
        meta = tool_named(tools, "zoo__board")["_meta"]
        assert naming.decode_resource_uri(meta["ui/resourceUri"]) == ("zoo", "ui://zoo/panel")
        # A backend that sent only the old spelling gets the new one too, so a host that
        # reads either is served.
        assert meta["ui"]["resourceUri"] == meta["ui/resourceUri"]
    finally:
        await harness.close()


async def test_the_listing_is_stable_across_repeated_calls(tmp_path) -> None:
    """The cached `_meta` must not be rewritten in place, or the second answer double-encodes."""
    servers = "servers:\n" + entry(
        "zoo",
        MOCK_TOOLS="board",
        MOCK_UI_TOOL="board",
        MOCK_UI_REF="ui://zoo/panel",
        MOCK_RESOURCES="ui://zoo/panel",
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        first = (await client.call("tools/list"))["tools"]
        second = (await client.call("tools/list"))["tools"]
        assert first == second
    finally:
        await harness.close()


async def test_a_reference_to_an_unpublished_panel_is_offered_and_reported(tmp_path) -> None:
    """Advisory, not enforced: the tool keeps its reference and health names the miss."""
    servers = "servers:\n" + entry(
        "zoo",
        MOCK_TOOLS="board",
        MOCK_UI_TOOL="board",
        MOCK_UI_REF="ui://zoo/missing",
        MOCK_RESOURCES="ui://zoo/panel",
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        # Populate the resource cache, which is the only thing `_publishes` consults.
        await client.call("resources/list")
        tools = (await client.call("tools/list"))["tools"]
        assert reference_in(tool_named(tools, "zoo__board")), "still offered"

        admin = await harness.connect("/admin", initialize=False)
        health = await admin.call("admin.health")
        zoo = next(b for b in health["backends"] if b["name"] == "zoo")
        assert zoo["unresolved_ui_templates"] == ["ui://zoo/missing"]
    finally:
        await harness.close()


async def test_a_hostile_csp_is_cleaned_before_it_reaches_a_client(tmp_path) -> None:
    """A downstream host turns this into a real header, so the `;` never leaves the gateway."""
    csp = '{\\"connectDomains\\": [\\"ok.example.com\\", \\"evil.example.com; script-src *\\"]}'
    servers = "servers:\n" + entry(
        "zoo", MOCK_TOOLS="board", MOCK_RESOURCES="ui://zoo/panel", MOCK_UI_CSP=csp
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        resources = (await client.call("resources/list"))["resources"]
        panel = next(r for r in resources if "ui%3A" in r["uri"])
        assert panel["_meta"]["ui"]["csp"]["connectDomains"] == ["ok.example.com"]
    finally:
        await harness.close()


async def test_a_reads_content_items_carry_the_public_uri(tmp_path) -> None:
    """A backend answers a read by naming its own URI, which the client never used.

    Harmless while nobody matched them up; not harmless once a panel is addressed that way,
    because a host cannot tell which request a content item answers.
    """
    servers = "servers:\n" + entry("zoo", MOCK_TOOLS="board", MOCK_RESOURCES="ui://zoo/panel")
    harness = await daemon(tmp_path, servers=servers)
    try:
        client = await harness.connect()
        listed = (await client.call("resources/list"))["resources"][0]["uri"]
        read = await client.call("resources/read", {"uri": listed})
        assert read["contents"][0]["uri"] == listed, "the answer must name the address asked for"
        assert naming.decode_resource_uri(read["contents"][0]["uri"]) == ("zoo", "ui://zoo/panel")
    finally:
        await harness.close()
