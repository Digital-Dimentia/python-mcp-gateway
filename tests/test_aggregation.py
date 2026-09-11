"""Many backends, one listing: namespacing, collisions, and what gets dropped."""

from __future__ import annotations

import sys
from pathlib import Path

from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable


def servers(**entries: dict) -> str:
    """Build a servers.yaml where every backend is the mock, configured by env knobs."""
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


async def test_tools_from_several_backends_are_namespaced(tmp_path) -> None:
    harness = await daemon(
        tmp_path,
        servers=servers(github={"MOCK_TOOLS": "create_issue,list_prs"}, fs={"MOCK_TOOLS": "read_file"}),
    )
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert "github__create_issue" in names
        assert "github__list_prs" in names
        assert "fs__read_file" in names
    finally:
        await harness.close()


async def test_two_backends_publishing_the_same_tool_name_both_survive(tmp_path) -> None:
    """The collision case. Prefixing means nothing is dropped and origin stays visible."""
    harness = await daemon(
        tmp_path, servers=servers(alpha={"MOCK_TOOLS": "search"}, beta={"MOCK_TOOLS": "search"})
    )
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert "alpha__search" in names
        assert "beta__search" in names
    finally:
        await harness.close()


async def test_a_tool_name_containing_the_separator_survives(tmp_path) -> None:
    """`github__create__issue` splits to `github` / `create__issue`, because server names
    may not contain `__`. That coupling is the whole reason routing needs no lookup table."""
    harness = await daemon(tmp_path, servers=servers(github={"MOCK_TOOL_SEP": "1"}))
    try:
        client = await harness.connect()
        assert "github__create__issue" in await client.tool_names()
        # The backend received `create__issue`, which is what proves the split was right.
        assert "mock:create__issue:" in await client.tool_text("github__create__issue")
    finally:
        await harness.close()


async def test_a_name_a_client_would_reject_is_dropped_and_recorded(tmp_path) -> None:
    """Offering a tool whose own client rejects the call is worse than not offering it."""
    import json

    harness = await daemon(tmp_path, servers=servers(srv={"MOCK_LONG_TOOL_NAME": "1"}))
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert not any(len(n) > 64 for n in names)

        health = json.loads(
            (
                await client.call(
                    "tools/call", {"name": "gateway__backend_health", "arguments": {"name": "srv"}}
                )
            )["content"][0]["text"]
        )
        assert health["skipped_tools"], "the drop must not be silent"
    finally:
        await harness.close()


async def test_the_meta_tools_come_first(tmp_path) -> None:
    """A model scanning a long list should meet gateway__list_backends before giving up."""
    harness = await daemon(tmp_path, servers=servers(a={}, b={}))
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert names[0].startswith("gateway__")
        assert any(n.startswith("a__") for n in names)
    finally:
        await harness.close()


async def test_the_listing_carries_no_cursor(tmp_path) -> None:
    """Backend pagination is already walked; a cursor here would be per-client state."""
    harness = await daemon(tmp_path, servers=servers(a={"MOCK_LIST_PAGES": "3"}))
    try:
        client = await harness.connect()
        result = await client.call("tools/list")
        assert "nextCursor" not in result
        assert "a__page1-tool" in [t["name"] for t in result["tools"]]
    finally:
        await harness.close()


async def test_a_backend_declaring_only_tools_contributes_no_prompts(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=servers(a={"MOCK_CAPABILITIES": "tools"}))
    try:
        client = await harness.connect()
        assert await client.tool_names()
        assert (await client.call("prompts/list"))["prompts"] == []
    finally:
        await harness.close()


async def test_a_failed_backend_does_not_empty_the_catalogue(tmp_path) -> None:
    """One broken backend must not make the model conclude the tools do not exist."""
    config = servers(good={"MOCK_TOOLS": "works"})
    config += "  broken:\n    command: /nonexistent/binary\n"
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        names = await client.tool_names()
        assert "good__works" in names
        assert not any(n.startswith("broken__") for n in names)
    finally:
        await harness.close()


async def test_a_disabled_backend_contributes_nothing_but_is_still_listed(tmp_path) -> None:
    config = servers(live={"MOCK_TOOLS": "here"})
    config += f"  parked:\n    command: {PY}\n    args: [\"{FIXTURE}\"]\n    enabled: false\n"
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        assert not any(n.startswith("parked__") for n in await client.tool_names())
        assert harness.gateway.backend("parked").status.value == "disabled"
    finally:
        await harness.close()


async def test_the_listing_is_cached_between_calls(tmp_path) -> None:
    """Otherwise the cheapest method in the protocol becomes the most expensive."""
    harness = await daemon(tmp_path, servers=servers(a={}))
    try:
        client = await harness.connect()
        await client.call("tools/list")
        assert harness.gateway.catalogue.counts("a")["tool_count"] is not None
        await client.call("tools/list")
        assert "a__echo" in await client.tool_names()
    finally:
        await harness.close()


async def test_a_backends_list_changed_drops_only_its_own_cache(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=servers(a={}, b={}))
    try:
        client = await harness.connect()
        await client.call("tools/list")
        catalogue = harness.gateway.catalogue
        assert catalogue.counts("a")["tool_count"] is not None
        catalogue.invalidate("a", kind="tools")
        assert catalogue.counts("a")["tool_count"] is None
        assert catalogue.counts("b")["tool_count"] is not None
    finally:
        await harness.close()


async def test_counts_distinguish_never_asked_from_empty(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=servers(a={}))
    try:
        assert harness.gateway.catalogue.counts("a")["tool_count"] is None
        client = await harness.connect()
        await client.call("tools/list")
        assert harness.gateway.catalogue.counts("a")["tool_count"] == 1
    finally:
        await harness.close()
