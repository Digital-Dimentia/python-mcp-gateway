"""`/ui`: the admin UI's static assets, served from the same port as the two sockets.

Driven over real HTTP against a real daemon rather than by calling `webui.response`
directly, because the thing worth proving is that `websockets`' `process_request` hook
answers a plain GET at all -- that is the whole mechanism, and a unit test of the function
would pass with the hook unwired.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import zipfile
from http.client import HTTPConnection
from pathlib import Path

import pytest

from mcp_gateway import webui
from tests.fixtures.ws_client import daemon

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "src" / "mcp_gateway" / "ui"


async def get(port: int, path: str):
    """One plain HTTP GET. Threaded, because `http.client` is blocking."""

    def fetch():
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    return await asyncio.to_thread(fetch)


async def test_the_index_is_served_at_the_mount_point(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        for path in ("/ui/", "/ui/index.html"):
            status, headers, body = await get(harness.port, path)
            assert status == 200, path
            assert headers["Content-Type"].startswith("text/html"), path
            assert b"<title>MCP Gateway</title>" in body, path
    finally:
        await harness.close()


async def test_a_bare_ui_redirects_to_the_trailing_slash(tmp_path) -> None:
    """Not cosmetic, and only a browser finds it.

    A page served *at* `/ui` has `/` as its base URL, so the browser resolves `style.css`
    to `/style.css` and gets a 404: an unstyled page with no JavaScript, from a URL that
    answered 200. `/ui` is what the startup banner used to print, so it was the first thing
    anybody typed. Found by loading the page in a real browser; no HTTP-level assertion
    about the HTML could have caught it.
    """
    harness = await daemon(tmp_path)
    try:
        status, headers, _body = await get(harness.port, "/ui")
        assert status == 308
        assert headers["Location"] == "/ui/"
    finally:
        await harness.close()


async def test_the_redirect_keeps_the_query_string(tmp_path) -> None:
    """Which is where the access key is, so dropping it would send the user to the gate."""
    harness = await daemon(tmp_path)
    try:
        status, headers, _body = await get(harness.port, "/ui?key=abc&x=1")
        assert status == 308
        assert headers["Location"] == "/ui/?key=abc&x=1"
    finally:
        await harness.close()


async def test_every_asset_is_served_with_its_content_type(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        for name, content_type in webui.ASSETS.items():
            status, headers, body = await get(harness.port, f"/ui/{name}")
            assert status == 200, name
            assert headers["Content-Type"] == content_type, name
            assert body, name
    finally:
        await harness.close()


async def test_the_allowlist_and_the_directory_agree(tmp_path) -> None:
    """The allowlist is the security boundary, so drift must be a red test, not a 404.

    `webui.py` resolves a request against `ASSETS` and never joins a path, which is what
    makes traversal unreachable rather than merely guarded. The cost of that is this
    assertion: a new file in `ui/` has to be named in the tuple or it is not served, and
    a deleted one has to leave it or it 500s.
    """
    on_disk = {p.name for p in ASSET_DIR.iterdir() if p.is_file() and p.suffix != ".py"}
    assert on_disk == set(webui.ASSETS), "src/mcp_gateway/ui/ and webui.ASSETS disagree"


@pytest.mark.parametrize(
    "path",
    [
        "/ui/nope.js",
        "/ui/../webui.py",
        "/ui/../../pyproject.toml",
        "/ui/%2e%2e/webui.py",
        "/ui/sub/app.js",
        "/ui/app.js/",
    ],
)
async def test_nothing_outside_the_allowlist_is_served(tmp_path, path) -> None:
    harness = await daemon(tmp_path)
    try:
        status, _headers, body = await get(harness.port, path)
        assert status == 404, path
        assert b"No such UI asset" in body, path
    finally:
        await harness.close()


async def test_the_assets_need_no_access_key(tmp_path) -> None:
    """A browser cannot key a navigation. See webui.md for why that is safe here."""
    harness = await daemon(tmp_path, access_key="s3cret")
    try:
        status, _headers, body = await get(harness.port, "/ui/")
        assert status == 200
        assert b"<title>MCP Gateway</title>" in body
        # ...and the sockets still do.
        with pytest.raises(Exception):
            await harness.connect("/admin", key=None, initialize=False)
    finally:
        await harness.close()


async def test_an_unknown_path_still_404s_and_names_the_ui(tmp_path) -> None:
    harness = await daemon(tmp_path)
    try:
        status, _headers, body = await get(harness.port, "/nope")
        assert status == 404
        assert b"/ui" in body
    finally:
        await harness.close()


async def test_the_assets_carry_a_policy_that_forbids_loading_anything_else(tmp_path) -> None:
    """An admin console for a credential store should work with no route off the machine."""
    harness = await daemon(tmp_path)
    try:
        _status, headers, _body = await get(harness.port, "/ui/")
        policy = headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert headers["Cache-Control"] == "no-store"
    finally:
        await harness.close()


async def test_the_page_loads_nothing_from_the_network(tmp_path) -> None:
    """The policy above is only a promise if the files keep it."""
    text = (ASSET_DIR / "index.html").read_text()
    assert "http://" not in text.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in text


def test_the_wheel_carries_the_assets() -> None:
    """Package data, not a checkout-only convenience. Skipped when nothing is built yet."""
    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if not wheels:
        pytest.skip("no wheel built; run `make build` first")
    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
    for asset in webui.ASSETS:
        assert f"mcp_gateway/ui/{asset}" in names, asset


def test_the_modules_parse() -> None:
    """`node --check` on each ES module, when node is around.

    The one automated thing worth saying about six files no Python test can exercise: they
    are syntactically valid ES modules. Skipped rather than required, because this project
    has no Node toolchain and is not acquiring one to run a parser.

    What this does *not* check is behaviour. That is what `examples/zoo_server.py` is for --
    one tool per JSON Schema construct, looked at in a browser. See webui.md.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name in webui.ASSETS:
        if not name.endswith(".js"):
            continue
        result = subprocess.run(
            [node, "--check", str(ASSET_DIR / name)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_the_url_carries_the_key_only_when_there_is_one() -> None:
    # Trailing slash: the canonical form, so the banner costs no redirect hop.
    assert webui.url("127.0.0.1", 8765) == "http://127.0.0.1:8765/ui/"
    assert webui.url("127.0.0.1", 8765, "abc") == "http://127.0.0.1:8765/ui/?key=abc"
    # A wildcard bind is not an address a browser can be told to visit.
    assert webui.url("0.0.0.0", 8765).startswith("http://127.0.0.1:")
