"""`admin.panel.open`: a capability URL for one HTML panel, from a real backend.

`tests/test_panels.py` covers the store and the endpoint. This file is about the mint, and
about the two properties that only a running gateway can show: that the bytes at the URL are
the *backend's* answer rather than anything a caller supplied, and that the URL dies with the
`/admin` connection that asked for it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from http.client import HTTPConnection
from pathlib import Path

from mcp_gateway import naming
from tests.fixtures.ws_client import daemon

PY = sys.executable
FIXTURE = str(Path(__file__).parent / "fixtures" / "mock_backend.py")

PANEL_HTML = "<!doctype html><title>board</title><script>parent.postMessage(1, '*')</script>"


def servers(**env: str) -> str:
    lines = ["servers:", "  zoo:", f"    command: {PY}", f'    args: ["{FIXTURE}"]', "    env:"]
    # `json.dumps` rather than quotes of our own: a hostile `csp` block is JSON with
    # quotes and braces in it, and a YAML value that has to be escaped by hand is a fixture
    # that will one day be testing its own quoting.
    lines += [f"      {key}: {json.dumps(value)}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


HTML_PANEL = dict(
    MOCK_NAME="zoo",
    MOCK_TOOLS="board",
    MOCK_UI_TOOL="board",
    MOCK_UI_REF="ui://zoo/panel",
    MOCK_RESOURCES="ui://zoo/panel",
    MOCK_UI_HTML=PANEL_HTML,
)


async def get(port: int, path: str):
    def fetch():
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    return await asyncio.to_thread(fetch)


def public_uri() -> str:
    return naming.encode_resource_uri("zoo", "ui://zoo/panel")


async def test_a_minted_url_serves_the_backends_own_document(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=servers(**HTML_PANEL))
    try:
        admin = await harness.connect("/admin", initialize=False)
        minted = await admin.call("admin.panel.open", {"uri": public_uri()})

        assert minted["url"].startswith("/panel/")
        assert minted["policy"].startswith("sandbox allow-scripts;")

        status, headers, body = await get(harness.port, minted["url"])
        assert status == 200
        assert body.decode() == PANEL_HTML
        assert headers["Content-Security-Policy"] == minted["policy"]
    finally:
        await harness.close()


async def test_the_tier_is_picked_by_mime_type_not_by_position(tmp_path) -> None:
    """The fixture answers with the declarative item first, on purpose."""
    harness = await daemon(tmp_path, servers=servers(**HTML_PANEL, MOCK_UI_READ_CSP="{}"))
    try:
        client = await harness.connect()
        read = await client.call("resources/read", {"uri": public_uri()})
        assert read["contents"][0]["mimeType"].startswith("application/json")

        admin = await harness.connect("/admin", initialize=False)
        minted = await admin.call("admin.panel.open", {"uri": public_uri()})
        _, _, body = await get(harness.port, minted["url"])
        assert body.decode() == PANEL_HTML
    finally:
        await harness.close()


async def test_a_backends_csp_reaches_the_header_already_sanitized(tmp_path) -> None:
    """Two entries, one of which is a second directive wearing a domain's clothes."""
    csp = json.dumps({"connectDomains": ["api.example.com", "evil.example; script-src *"]})
    harness = await daemon(tmp_path, servers=servers(**HTML_PANEL, MOCK_UI_READ_CSP=csp))
    try:
        admin = await harness.connect("/admin", initialize=False)
        minted = await admin.call("admin.panel.open", {"uri": public_uri()})

        assert "connect-src api.example.com" in minted["policy"]
        assert "evil.example" not in minted["policy"]
        # One `script-src`, and it is the one this gateway wrote.
        assert minted["policy"].count("script-src") == 1
    finally:
        await harness.close()


async def test_a_resource_that_is_not_an_html_panel_is_refused(tmp_path) -> None:
    """The declarative tier needs no URL: the page already has the document."""
    declarative = dict(HTML_PANEL)
    del declarative["MOCK_UI_HTML"]
    harness = await daemon(tmp_path, servers=servers(**declarative, MOCK_UI_READ_CSP="{}"))
    try:
        admin = await harness.connect("/admin", initialize=False)
        answer = await admin.request("admin.panel.open", {"uri": public_uri()})

        assert "error" in answer
        assert harness.gateway.panels.__len__() == 0
    finally:
        await harness.close()


async def test_a_uri_naming_nothing_is_an_error_rather_than_an_empty_panel(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=servers(**HTML_PANEL))
    try:
        admin = await harness.connect("/admin", initialize=False)
        for uri in ("", naming.encode_resource_uri("zoo", "ui://zoo/nope"), "not a uri"):
            answer = await admin.request("admin.panel.open", {"uri": uri})
            assert "error" in answer, uri
    finally:
        await harness.close()


async def test_closing_the_tab_takes_the_url_with_it(tmp_path) -> None:
    """A capability URL outliving the socket that authenticated it would be the bug."""
    harness = await daemon(tmp_path, servers=servers(**HTML_PANEL))
    try:
        admin = await harness.connect("/admin", initialize=False)
        minted = await admin.call("admin.panel.open", {"uri": public_uri()})
        await admin.close()

        # The close is seen by the daemon on its own schedule; wait for it rather than
        # racing it, because what is being asserted is *that* it happens.
        for _ in range(100):
            if len(harness.gateway.panels) == 0:
                break
            await asyncio.sleep(0.02)

        assert len(harness.gateway.panels) == 0
        status, _, _ = await get(harness.port, minted["url"])
        assert status == 404
    finally:
        await harness.close()
