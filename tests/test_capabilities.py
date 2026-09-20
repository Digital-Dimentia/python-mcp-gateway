"""Every advertised capability must be answerable, and the manifest is how we know."""

from __future__ import annotations

from mcp_gateway import protocol
from tests.fixtures.ws_client import daemon


def test_every_advertised_key_has_a_manifest_row() -> None:
    """Declaring something nothing answers entitles a peer to a request that will fail."""
    documented = {capability.key for capability in protocol.CAPABILITY_MANIFEST}
    assert set(protocol.advertised_capabilities()) <= documented


def test_every_manifest_row_names_what_answers_it() -> None:
    for capability in protocol.CAPABILITY_MANIFEST:
        assert capability.answered_by, capability.key


def test_subscribe_is_advertised_because_something_implements_it_now() -> None:
    """It was `False` while nothing answered `resources/subscribe`, and that was the honest
    value then. `router.subscribe_resource` answers it now, so the flag is a row in the
    manifest like every other -- and a backend that cannot be subscribed to refuses its own
    URI, which is a per-resource answer rather than a claim about the gateway."""
    assert protocol.advertised_capabilities()["resources"]["subscribe"] is True
    assert protocol.RESOURCES_SUBSCRIBE in ("resources/subscribe",)


async def test_the_block_does_not_depend_on_which_backends_are_configured(tmp_path) -> None:
    """The point of advertising unconditionally.

    MCP capabilities are fixed at `initialize`, so a backend coming up later via reload
    could never make `prompts` *appear* on an already-negotiated connection. A block that
    varied with backend startup would also differ between two runs of one config.
    """
    other = tmp_path / "other"
    other.mkdir()
    empty = await daemon(tmp_path)
    populated = await daemon(other, servers="servers:\n  a:\n    command: /usr/bin/true\n")
    try:
        one = await empty.connect(initialize=False)
        block_empty = (await one.initialize())["capabilities"]
        two = await populated.connect(initialize=False)
        block_populated = (await two.initialize())["capabilities"]
        assert block_empty == block_populated
    finally:
        await empty.close()
        await populated.close()


async def test_a_gateway_with_no_prompt_backends_returns_an_empty_list(tmp_path) -> None:
    """Which is what a real server with no prompts returns. Nobody is stranded on -32601."""
    harness = await daemon(tmp_path)
    try:
        client = await harness.connect()
        assert await client.call("prompts/list") == {"prompts": []}
        assert await client.call("resources/list") == {"resources": []}
        assert await client.call("resources/templates/list") == {"resourceTemplates": []}
    finally:
        await harness.close()


def test_negotiation_echoes_a_supported_proposal() -> None:
    for version in protocol.SUPPORTED_MCP_PROTOCOL_VERSIONS:
        assert protocol.negotiate_version(version) == (version, True)


def test_negotiation_counters_an_unsupported_proposal() -> None:
    assert protocol.negotiate_version("1999-01-01") == (protocol.MCP_PROTOCOL_VERSION, False)


def test_a_missing_version_reads_as_the_older_revision() -> None:
    """2024-11-05 servers omit it; refusing them would drop working backends."""
    assert protocol.negotiate_version(None) == ("2024-11-05", True)


# --- what a declared capability entitles a server to ------------------------------


def test_only_some_declared_capabilities_entitle_the_server_to_a_request() -> None:
    """`roots` and `elicitation` buy the server a method to call back with. Nothing else
    here does, and `_declared_capabilities` may only refuse a block over the ones that do.
    """
    from mcp_gateway.mcp_stdio import MCPClientCapabilities

    assert MCPClientCapabilities().requestable() == frozenset()
    assert MCPClientCapabilities(roots=True).requestable() == {"roots"}
    assert MCPClientCapabilities(elicitation=True).requestable() == {"elicitation"}
    assert MCPClientCapabilities(roots=True, elicitation=True).requestable() == {
        "roots",
        "elicitation",
    }


def test_a_requestable_capability_without_a_handler_is_refused_before_the_wire() -> None:
    """A conformance bug in this process, caught here rather than as a late `-32601`."""
    import pytest

    from mcp_gateway.mcp_stdio import MCPClientCapabilities, MCPStdioClient

    client = MCPStdioClient(
        command=["true"],
        client_capabilities=MCPClientCapabilities(roots=True),
    )
    with pytest.raises(RuntimeError, match="roots"):
        client._declared_capabilities()


def test_a_capability_that_entitles_no_request_needs_no_handler() -> None:
    """The MCP Apps extension is the live example: it tells the server what this client can
    render, and the server answers by putting `_meta` on a tool -- never by calling back.

    Written against a synthetic non-requestable key rather than the extension itself, so it
    keeps testing the rule after the extension's own spelling changes.
    """
    from mcp_gateway.mcp_stdio import MCPClientCapabilities, MCPStdioClient

    class Rendering(MCPClientCapabilities):
        def to_wire(self) -> dict:
            return {"example.test/rendering": {"mimeTypes": ["text/plain"]}}

    client = MCPStdioClient(command=["true"], client_capabilities=Rendering())
    assert client._declared_capabilities() == {
        "example.test/rendering": {"mimeTypes": ["text/plain"]}
    }


# --- MCP Apps: what the gateway tells its backends --------------------------------


def test_the_ui_extension_is_not_advertised_upward_because_it_is_a_client_capability() -> None:
    """The gateway is the *server* to its clients, so there is nothing to advertise and the
    manifest is untouched. A row here would be the mistake, not the fix."""
    block = protocol.advertised_capabilities()
    assert "extensions" not in block
    assert protocol.UI_EXTENSION_ID not in block
    assert all(capability.key != "extensions" for capability in protocol.CAPABILITY_MANIFEST)


def test_the_ui_extension_rides_under_extensions_not_at_the_top_level() -> None:
    from mcp_gateway.mcp_stdio import MCPClientCapabilities

    wire = MCPClientCapabilities(ui_app_mime_types=("text/plain",)).to_wire()
    assert wire == {"extensions": {protocol.UI_EXTENSION_ID: {"mimeTypes": ["text/plain"]}}}


def test_no_declared_mime_types_means_no_extension_key_at_all() -> None:
    """Absent reads as unsupported; an empty list would read as supported-but-nothing."""
    from mcp_gateway.mcp_stdio import MCPClientCapabilities

    assert MCPClientCapabilities().to_wire() == {}


def test_the_ui_extension_does_not_require_a_server_request_handler() -> None:
    """It entitles a backend to no request, so the guard must not fire on it."""
    from mcp_gateway.mcp_stdio import MCPClientCapabilities, MCPStdioClient

    client = MCPStdioClient(
        command=["true"],
        client_capabilities=MCPClientCapabilities(ui_app_mime_types=("text/plain",)),
    )
    assert protocol.UI_EXTENSION_ID in client._declared_capabilities()["extensions"]


def _union_of(*capability_blocks) -> tuple[str, ...]:
    """`Gateway._ui_app_mime_types` reads only `self._sessions`, so a stub is the whole
    fixture. Keeps this about the union rule rather than about starting a daemon."""
    from types import SimpleNamespace

    from mcp_gateway.gateway import Gateway

    stub = SimpleNamespace(
        _sessions=[SimpleNamespace(client_capabilities=block) for block in capability_blocks]
    )
    return Gateway._ui_app_mime_types(stub)


def _declares(*mime_types: str) -> dict:
    return {"extensions": {protocol.UI_EXTENSION_ID: {"mimeTypes": list(mime_types)}}}


def test_a_backend_is_told_what_the_attached_clients_can_render() -> None:
    assert _union_of(_declares("text/plain")) == ("text/plain",)


def test_two_clients_that_render_different_things_are_unioned() -> None:
    """A backend offering for the union lets each host take the one it understands."""
    assert _union_of(_declares("a/x"), _declares("b/y")) == ("a/x", "b/y")


def test_the_union_is_sorted_so_a_restart_is_not_mistaken_for_a_change() -> None:
    assert _union_of(_declares("z/z", "a/a")) == ("a/a", "z/z")


def test_a_client_that_declares_nothing_contributes_nothing() -> None:
    assert _union_of({}, {"roots": {}}) == ()


def test_no_clients_at_all_means_no_extension() -> None:
    assert _union_of() == ()


def test_a_malformed_extension_block_is_ignored_rather_than_trusted() -> None:
    """It arrives from a client, so every level of it is someone else's input."""
    assert _union_of({"extensions": "nonsense"}) == ()
    assert _union_of({"extensions": {protocol.UI_EXTENSION_ID: "nonsense"}}) == ()
    assert _union_of({"extensions": {protocol.UI_EXTENSION_ID: {"mimeTypes": "a/x"}}}) == ()
    assert _union_of({"extensions": {protocol.UI_EXTENSION_ID: {"mimeTypes": [1, "a/x"]}}}) == (
        "a/x",
    )
