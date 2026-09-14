"""White labelling: the `branding:` block, and the three surfaces it reaches.

The block is display-only, which is exactly why it needs pinning: a wrong title is obvious
and a *silently ignored* one is not. Every refusal below is a refusal the operator would
otherwise meet as "my logo did not appear and I do not know why".
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from mcp_gateway import branding
from mcp_gateway.config import ConfigError, parse

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16"/></svg>'

CATALOGUE = """
version: 1
servers:
  demo:
    command: echo
    args: ["hi"]
"""


def write(tmp_path: Path, block: str, *, icon: bytes | None = SVG, name: str = "logo.svg") -> Path:
    if icon is not None:
        (tmp_path / name).write_bytes(icon)
    path = tmp_path / "servers.yaml"
    path.write_text(block + CATALOGUE)
    return path


def load(tmp_path: Path, block: str, **kwargs) -> branding.Branding:
    path = write(tmp_path, block, **kwargs)
    return parse(path.read_text(), source=path).branding


# --- the default ------------------------------------------------------------------------


def test_a_catalogue_without_a_branding_block_is_unbranded(tmp_path: Path) -> None:
    """The whole feature has to be invisible when unused.

    `serverInfo.name` in particular is a wire value that clients key their own config off,
    so the default is not merely "some default" -- it is the exact string every released
    version has reported.
    """
    brand = load(tmp_path, "", icon=None)
    assert brand == branding.DEFAULT
    assert brand.name == "mcp-gateway"
    assert brand.title == "MCP Gateway"
    assert brand.icon_data_uri() is None
    assert not brand.customised


# --- the two names ----------------------------------------------------------------------


def test_title_and_name_are_separate_fields(tmp_path: Path) -> None:
    brand = load(
        tmp_path,
        'branding:\n  title: "Acme Internal Tools"\n  name: acme-tools\n',
        icon=None,
    )
    assert brand.title == "Acme Internal Tools"
    assert brand.name == "acme-tools"
    assert brand.customised


def test_a_name_with_whitespace_is_refused_and_points_at_title(tmp_path: Path) -> None:
    """The message matters more than the refusal.

    Someone who writes `name: Acme Internal Tools` wants a display string; the fix is one
    field over, and an error that only said "invalid" would send them to the docs instead.
    """
    with pytest.raises(ConfigError) as refusal:
        load(tmp_path, 'branding:\n  name: "Acme Internal Tools"\n', icon=None)
    assert "title" in str(refusal.value)


@pytest.mark.parametrize(
    "block",
    [
        'branding:\n  title: ""\n',
        "branding:\n  title: 7\n",
        "branding:\n  colour: red\n",
        "branding: nonsense\n",
    ],
)
def test_a_branding_block_that_cannot_be_honoured_is_a_config_error(tmp_path: Path, block: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, block, icon=None)


def test_a_control_character_in_a_title_is_refused(tmp_path: Path) -> None:
    """It reaches a window title bar, a log line and a terminal banner."""
    with pytest.raises(ConfigError):
        load(tmp_path, 'branding:\n  title: "Acme\\u001b[31m"\n', icon=None)


def test_an_unknown_key_names_the_ones_that_are_allowed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refusal:
        load(tmp_path, "branding:\n  logo: ./logo.svg\n", icon=None)
    message = str(refusal.value)
    assert "logo" in message and "icon" in message


# --- the icon ---------------------------------------------------------------------------


def test_the_icon_resolves_against_the_catalogue_not_the_cwd(tmp_path: Path) -> None:
    """A daemon is started by launchd, by a container entrypoint and by hand.

    Those are three working directories for one unchanged config file, so a relative path
    that meant "beside servers.yaml" in a checkout has to keep meaning it everywhere.
    """
    brand = load(tmp_path, "branding:\n  icon: ./logo.svg\n")
    assert brand.icon_path == (tmp_path / "logo.svg").resolve()


def test_the_icon_is_served_as_a_data_uri(tmp_path: Path) -> None:
    """Not a URL. The desktop shell's CSP cannot fetch one -- see branding.md."""
    brand = load(tmp_path, "branding:\n  icon: ./logo.svg\n")
    uri = brand.icon_data_uri()
    assert uri is not None
    head, encoded = uri.split(",", 1)
    assert head == "data:image/svg+xml;base64"
    assert base64.b64decode(encoded) == SVG
    assert brand.describe()["icon"] == uri


def test_the_icon_is_re_read_rather_than_cached(tmp_path: Path) -> None:
    """Replacing the logo and pressing Reload is the whole edit loop for this file."""
    brand = load(tmp_path, "branding:\n  icon: ./logo.svg\n")
    before = brand.icon_data_uri()
    (tmp_path / "logo.svg").write_bytes(SVG.replace(b"<rect", b"<circle"))
    assert brand.icon_data_uri() != before


def test_a_missing_icon_is_refused_at_parse_time(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refusal:
        load(tmp_path, "branding:\n  icon: ./absent.svg\n", icon=None)
    assert "absent.svg" in str(refusal.value)


def test_an_icon_this_gateway_cannot_serve_names_what_it_can(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as refusal:
        load(tmp_path, "branding:\n  icon: ./logo.pdf\n", name="logo.pdf")
    assert ".svg" in str(refusal.value)


def test_an_oversized_icon_is_refused(tmp_path: Path) -> None:
    """The bytes ride along on every `admin.status`, so the cap is not decoration."""
    with pytest.raises(ConfigError) as refusal:
        load(tmp_path, "branding:\n  icon: ./logo.png\n",
             icon=b"\x89PNG" + b"\0" * branding.MAX_ICON_BYTES, name="logo.png")
    assert str(branding.MAX_ICON_BYTES) in str(refusal.value)


def test_an_icon_that_disappears_under_a_running_daemon_degrades_to_no_icon(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Serve time is forgiving where parse time is strict.

    `icon_data_uri` runs while answering `admin.status`. A deleted file must cost the page
    its logo, never its footer, its socket pills and its reload button.
    """
    brand = load(tmp_path, "branding:\n  icon: ./logo.svg\n")
    (tmp_path / "logo.svg").unlink()
    with caplog.at_level("WARNING"):
        assert brand.icon_data_uri() is None
    assert "logo.svg" in caplog.text
    assert brand.describe()["title"] == "MCP Gateway"


# --- the desktop overlay ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Acme Internal Tools", "Acme-Internal-Tools"),
        ("MCP Gateway", "MCP-Gateway"),
        ("Acme / Tools™", "Acme-Tools"),
        ("   ", "MCP-Gateway"),
    ],
)
def test_the_bundle_name_needs_no_quoting(title: str, expected: str) -> None:
    """`productName` becomes a path that gets typed, pasted and handed to `open`.

    The same rule `tests/test_desktop_layout.py` pins for the committed config, applied to
    a title nobody here chose. The window keeps the prose.
    """
    import scripts.brand_desktop as brand_desktop

    assert brand_desktop.product_name(title) == expected


def test_the_overlay_changes_only_what_a_rebrand_changes() -> None:
    """A merge document, not a replacement config.

    Everything this file does not mention -- the CSP, the resources, the frontend path --
    has to keep coming from the committed `tauri.conf.json`, which is where the tests that
    pin those live.
    """
    import scripts.brand_desktop as brand_desktop

    overlay = brand_desktop.overlay_for("Acme Internal Tools", icons=False)
    assert overlay == {
        "productName": "Acme-Internal-Tools",
        "app": {"windows": [{"title": "Acme Internal Tools"}]},
    }
    assert "icon" in brand_desktop.overlay_for("Acme", icons=True)["bundle"]


# --- the two sockets --------------------------------------------------------------------


BRANDED = """
version: 1
branding:
  title: "Acme Internal Tools"
  name: acme-tools
  icon: ./logo.svg
servers: {}
"""


async def test_a_branded_gateway_introduces_itself_as_one(tmp_path: Path) -> None:
    """`serverInfo` is the one branded surface a model sees.

    `name` is what a client keys its config off; `title` is the display string beside it.
    Both come from the block, and the tool namespace does **not** -- a backend is still
    `<server>__<tool>` no matter what the deployment is called. See branding.md.
    """
    from tests.fixtures.ws_client import daemon

    (tmp_path / "logo.svg").write_bytes(SVG)
    harness = await daemon(tmp_path, servers=BRANDED)
    try:
        client = await harness.connect(initialize=False)
        info = (await client.initialize())["serverInfo"]
        assert info["name"] == "acme-tools"
        assert info["title"] == "Acme Internal Tools"

        admin = await harness.connect("/admin", initialize=False)
        brand = (await admin.call("admin.status"))["branding"]
        assert brand["title"] == "Acme Internal Tools"
        assert brand["name"] == "acme-tools"
        # Inline, because the desktop shell cannot fetch a URL for it.
        assert brand["icon"].startswith("data:image/svg+xml;base64,")
    finally:
        await harness.close()


async def test_an_unbranded_gateway_reports_the_name_it_always_has(tmp_path: Path) -> None:
    """The compatibility half. A client pinned to `mcp-gateway` must keep working."""
    from tests.fixtures.ws_client import daemon

    harness = await daemon(tmp_path)
    try:
        client = await harness.connect(initialize=False)
        assert (await client.initialize())["serverInfo"]["name"] == "mcp-gateway"

        admin = await harness.connect("/admin", initialize=False)
        assert (await admin.call("admin.status"))["branding"]["icon"] is None
    finally:
        await harness.close()
