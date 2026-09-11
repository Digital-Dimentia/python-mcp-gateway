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


def test_subscribe_is_advertised_false_because_nothing_implements_it() -> None:
    assert protocol.advertised_capabilities()["resources"]["subscribe"] is False


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
