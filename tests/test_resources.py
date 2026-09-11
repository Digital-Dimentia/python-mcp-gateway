"""Resources, where namespacing cannot be a name prefix and has to be a URI scheme."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_gateway import errors
from mcp_gateway.naming import decode_resource_uri, encode_resource_uri
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  docs:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "docs"
      MOCK_RESOURCES: "file:///README.md,file:///guide.md"
      MOCK_TEMPLATES: "file:///docs/{{path}}"
  code:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "code"
      MOCK_RESOURCES: "file:///README.md"
"""


async def test_resource_uris_are_rewritten_into_the_gateways_address_space(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        uris = [r["uri"] for r in (await client.call("resources/list"))["resources"]]
        assert all(uri.startswith("mcpgw://") for uri in uris)
        assert decode_resource_uri(uris[0])[1].startswith("file:///")
    finally:
        await harness.close()


async def test_two_backends_publishing_the_same_uri_get_distinct_addresses(tmp_path) -> None:
    """The reason rewriting exists at all.

    Two filesystem servers rooted differently both publish `file:///README.md`. Passing the
    original through would leave dictionary ordering to decide which one answers a read.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        uris = [r["uri"] for r in (await client.call("resources/list"))["resources"]]
        readmes = [u for u in uris if "README" in decode_resource_uri(u)[1]]
        assert len(readmes) == 2
        assert len({decode_resource_uri(u)[0] for u in readmes}) == 2

        for uri in readmes:
            server = decode_resource_uri(uri)[0]
            contents = await client.call("resources/read", {"uri": uri})
            assert contents["contents"][0]["text"].startswith(f"{server}:")
    finally:
        await harness.close()


async def test_a_template_keeps_its_rfc6570_expression(tmp_path) -> None:
    """`safe="{}"` is what lets the client still expand a forwarded uriTemplate."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        templates = (await client.call("resources/templates/list"))["resourceTemplates"]
        expressions = [t["uriTemplate"] for t in templates]
        assert any("{path}" in expression for expression in expressions)
        assert all(expression.startswith("mcpgw://") for expression in expressions)
    finally:
        await harness.close()


async def test_display_names_are_prefixed_for_a_person_not_for_code(tmp_path) -> None:
    """`docs/README.md`, with a slash: read in a picker, never split by code."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        names = [r.get("name", "") for r in (await client.call("resources/list"))["resources"]]
        assert any(name.startswith("docs/") for name in names)
    finally:
        await harness.close()


async def test_reading_reaches_the_right_backend(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        uri = encode_resource_uri("docs", "file:///guide.md")
        contents = await client.call("resources/read", {"uri": uri})
        assert contents["contents"][0]["text"] == "docs:file:///guide.md"
    finally:
        await harness.close()


async def test_a_uri_naming_no_backend_is_resource_not_found(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request(
            "resources/read", {"uri": encode_resource_uri("nope", "file:///x")}
        )
        assert response["error"]["code"] == errors.RESOURCE_NOT_FOUND
    finally:
        await harness.close()


async def test_a_uri_that_is_not_ours_is_resource_not_found(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("resources/read", {"uri": "file:///etc/hosts"})
        assert response["error"]["code"] == errors.RESOURCE_NOT_FOUND
    finally:
        await harness.close()


async def test_an_unreadable_uri_on_a_real_backend_is_the_backends_answer(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request(
            "resources/read", {"uri": encode_resource_uri("docs", "file:///gone.md")}
        )
        assert response["error"]["data"]["source"] == errors.BACKEND_SOURCE
        assert response["error"]["data"]["backend"] == "docs"
    finally:
        await harness.close()


async def test_resources_read_without_a_uri_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("resources/read", {})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()
