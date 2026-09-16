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
from xml.etree import ElementTree
from http.client import HTTPConnection
from pathlib import Path

import pytest

from mcp_gateway import webui
from tests.fixtures.ws_client import daemon

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "src" / "mcp_gateway_ui"


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
    assertion: a new file in `src/mcp_gateway_ui/` has to be named in the tuple or it is
    not served, and a deleted one has to leave it or it 500s.
    """
    on_disk = {p.name for p in ASSET_DIR.iterdir() if p.is_file() and p.suffix != ".py"}
    assert on_disk == set(webui.ASSETS), "src/mcp_gateway_ui/ and webui.ASSETS disagree"


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
        assert f"mcp_gateway_ui/{asset}" in names, asset


def test_the_screen_modules_do_not_import_the_frame() -> None:
    """The seam is one-way, and this is the only place that can say so.

    `app.js` imports the screen modules and hands each one what it may touch -- the state it
    renders, and for the variables column a named port of everything it may do to an open
    form. Importing back the other way would work, and it is exactly what must not happen:
    the cycle is the lesser problem, and a screen reaching into the frame's variables is the
    real one, because it is what makes a screen impossible to move, test or delete on its
    own. Nothing about that rule is visible from inside either file, so it is checked here.

    Prose in these files mentions `app.js` constantly, which is why this matches the import
    specifier rather than the name.
    """
    for name in ("variables.js", "detail.js", "primitives.js", "results.js", "naming.js", "screen_about.js", "screen_basics.js"):
        text = (ASSET_DIR / name).read_text()
        assert "from './app.js'" not in text, name


def test_the_modules_parse() -> None:
    """`node --check` on each ES module, when node is around.

    The cheapest of the three layers, and the only one that needs nothing installed: they are
    syntactically valid ES modules. `tests/test_webui_js.py` is the layer that runs them, and
    `examples/zoo_server.py` in a browser is the one that says they render. See webui.md.
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


async def test_the_stock_mark_is_served_as_an_image(tmp_path) -> None:
    """The default logo is a file on `/ui`, not an emoji in a data URI.

    It is the favicon, the header mark, and the thing a deployment swaps -- so it has to be
    fetchable by a browser *and* stageable into the desktop shell, which is what being an
    asset in the allowlist buys. A configured `branding.icon` never comes through here: that
    file lives outside this package and arrives inline over `/admin`. See `branding.md`.
    """
    harness = await daemon(tmp_path)
    try:
        status, headers, body = await get(harness.port, "/ui/logo.svg")
        assert status == 200
        assert headers["Content-Type"] == "image/svg+xml"
        assert body.lstrip().startswith(b"<svg")
        # No <style> block: an SVG in an <img> is its own document, served under this
        # module's `default-src 'none'` policy, where inline CSS is refused.
        assert b"<style" not in body
    finally:
        await harness.close()


def test_the_stock_mark_is_well_formed_xml() -> None:
    """An SVG in an `<img>` is parsed as XML, not as HTML, and XML is unforgiving.

    A `--` inside a comment is the trap: illegal in XML, tolerated nowhere, and the symptom
    is not an error but an image that silently does not appear. It happened while this file
    was being written, in a comment explaining the CSS custom property the colour came from.
    Every other well-formedness slip fails the same silent way, so the check is the whole
    parse rather than that one rule.
    """
    root = ElementTree.fromstring((ASSET_DIR / "logo.svg").read_text())
    assert root.tag.endswith("svg")
    assert root.get("viewBox"), "a mark with no viewBox cannot be scaled to a favicon"
    for circle in (e for e in root.iter() if e.tag.endswith("circle")):
        cx, cy, r = (float(circle.get(k)) for k in ("cx", "cy", "r"))
        assert 0 <= cx - r and cx + r <= 32, circle.attrib
        assert 0 <= cy - r and cy + r <= 32, circle.attrib


def test_the_markup_wears_the_stock_mark_before_any_socket_answers() -> None:
    """`index.html` names `logo.svg` twice: the favicon, and the header image.

    Pinned because the fallback in `app.js` reads the second one off the DOM -- the markup
    is the single place the stock mark is written down, and a rename here that missed one of
    the two would leave a broken image until the first `admin.status` arrived.
    """
    markup = (ASSET_DIR / "index.html").read_text()
    assert 'id="brand-favicon" href="logo.svg"' in markup
    assert 'id="brand-icon" class="brand-icon" src="logo.svg"' in markup
