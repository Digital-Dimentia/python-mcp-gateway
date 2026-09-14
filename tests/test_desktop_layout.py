"""The desktop shell's layout: one copy of the UI, and a config that says what it does.

Two things are asserted here, and both are invariants a person cannot hold in their head.

**There is one admin UI.** `desktop/.staging/ui` is a rebuilt view of `src/mcp_gateway/ui/`,
not a fork. Nothing enforces that except this file: the day someone edits a staged file
because it was the one open in the editor, the copy stops being a copy and the browser build
and the app start to differ in ways nobody looks for.

**The window cannot reach the network.** The whole design -- the host owning the sockets,
the key never entering JavaScript -- rests on the page having no way out except the IPC
channel. `tauri.conf.json` is where that is declared, so it is where it is checked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI = REPO_ROOT / "src" / "mcp_gateway" / "ui"
DESKTOP = REPO_ROOT / "desktop"
TAURI = DESKTOP / "src-tauri"
STAGING = DESKTOP / ".staging" / "ui"

sys.path.insert(0, str(REPO_ROOT / "src"))
from mcp_gateway import webui  # noqa: E402
sys.path.pop(0)


def config() -> dict:
    return json.loads((TAURI / "tauri.conf.json").read_text())


def stage(mode: str) -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "stage_ui.py"), "--mode", mode],
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )


# --- one copy of the UI -----------------------------------------------------------------


def test_a_copy_stage_is_byte_identical_to_the_source() -> None:
    """Every staged file came from `src/mcp_gateway/ui/` and was not touched on the way."""
    stage("copy")
    try:
        staged = {p.name: p.read_bytes() for p in STAGING.iterdir() if p.is_file()}
        assert staged, "nothing was staged"
        for name, content in staged.items():
            assert content == (UI / name).read_bytes(), name
    finally:
        stage("link")


def test_the_stage_carries_the_allowlist_and_nothing_else() -> None:
    """`webui.ASSETS` is the manifest, so the bundle cannot be more generous than the HTTP
    server is. A file sitting in `ui/` that is not served over the wire must not reach the
    app either -- `__init__.py` is exactly such a file, and it is there for packaging."""
    stage("copy")
    try:
        assert {p.name for p in STAGING.iterdir() if p.is_file()} == set(webui.ASSETS)
    finally:
        stage("link")


def test_the_shim_ships_to_both_hosts() -> None:
    """One file, two hosts. It is in the allowlist because the page loads it either way."""
    assert "tauri-transport.js" in webui.ASSETS
    assert (UI / "tauri-transport.js").is_file()
    index = (UI / "index.html").read_text()
    # Before app.js: it has to swap the transport before anything connects.
    assert index.index("tauri-transport.js") < index.index('src="app.js"')


def test_the_staging_directory_is_not_committed() -> None:
    """It is generated. A committed one is the second copy this file exists to prevent."""
    tracked = subprocess.run(
        ["git", "ls-files", "desktop/.staging"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert tracked.stdout.strip() == "", tracked.stdout


# --- what the window is allowed to do -------------------------------------------------------


def test_the_window_may_not_open_a_socket_of_its_own() -> None:
    """The CSP is where "the page cannot reach the network" stops being a claim.

    Narrower than `webui.py`'s policy on purpose: that one has to allow `ws:`/`wss:` because
    the browser build's page really does dial out. This one must not.
    """
    csp = config()["app"]["security"]["csp"]
    assert "ws:" not in csp and "wss:" not in csp, csp
    assert "connect-src ipc: http://ipc.localhost" in csp, csp
    # The same skeleton as `webui.py`'s: nothing loads unless the page shipped it.
    for directive in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'"):
        assert directive in csp, csp


def test_the_frontend_is_the_staged_view_and_not_a_second_copy() -> None:
    assert config()["build"]["frontendDist"] == "../.staging/ui"


def test_the_bundle_carries_the_interpreter_and_the_seed_files() -> None:
    resources = config()["bundle"]["resources"]
    assert resources["resources/python"] == "python"
    assert resources["seed"] == "seed"


def test_the_bundle_name_needs_no_quoting() -> None:
    """`MCP-Gateway.app`, not `MCP Gateway.app`.

    A space is legal and Apple uses them freely, but this bundle's path gets typed, pasted
    into scripts, and passed to `open`. Unquoted, a space there fails in a way that reads as
    a broken app rather than a broken command line — which is exactly how it presented the
    first time. The window title keeps the space, because nothing has to quote prose.
    """
    assert " " not in config()["productName"]
    assert config()["productName"] == "MCP-Gateway"


def test_the_bundle_is_signed_even_without_a_certificate() -> None:
    """Ad-hoc, which needs no certificate, but a signature all the same.

    Without `signingIdentity`, Tauri leaves the `.app` with no `_CodeSignature` at all: the
    main binary carries only the linker's ad-hoc signature, and `codesign --verify` reports
    "code has no resources but signature indicates they must be present". On Apple Silicon
    that is a bundle macOS is within its rights to refuse, and the failure surfaces as a
    LaunchServices error code with nothing in it that names the cause.

    `-` is the ad-hoc identity. Real signing and notarisation is its own bead; this is the
    floor, and the floor is cheap.
    """
    assert config()["bundle"]["macOS"]["signingIdentity"] == "-"


def test_the_app_names_the_same_deployment_as_the_launchd_job() -> None:
    """Both name one thing on one machine; drifting would give it two data directories."""
    plist = (REPO_ROOT / "scripts" / "com.dbuschman7.mcp-gateway.plist").read_text()
    assert config()["identifier"] in plist


def test_the_global_tauri_api_is_on_because_the_ui_has_no_build_step() -> None:
    """`tauri-transport.js` reads `window.__TAURI__` rather than importing a package.

    The admin UI is ES modules a browser loads directly, with no bundler and no npm
    dependency -- `webui.md` is emphatic about it. An `@tauri-apps/api` import would put a
    build step in front of that directory for one file's sake.
    """
    assert config()["app"]["withGlobalTauri"] is True
    shim = (UI / "tauri-transport.js").read_text()
    assert "window.__TAURI__" in shim
    assert "@tauri-apps/api" not in shim


# --- the two halves of the parser stay twinned -------------------------------------------


def test_the_rust_and_python_port_file_readers_are_twinned() -> None:
    """Each names the other; this fails if one is moved or renamed and the note goes stale."""
    rust = (TAURI / "src" / "supervisor.rs").read_text()
    python = (REPO_ROOT / "tests" / "test_desktop_contract.py").read_text()
    assert "portfile.py" in rust
    assert "supervisor.rs" in python


def test_the_shell_no_longer_reads_the_port_off_a_log_line() -> None:
    """The point of python-mcp-gateway-9p3. A log line is a sentence written for a human,
    and the moment something parses it for a port it can never be reworded again -- so this
    fails if the scrape comes back rather than only if the file mechanism breaks."""
    rust = (TAURI / "src" / "supervisor.rs").read_text()
    code = "\n".join(line for line in rust.splitlines() if not line.lstrip().startswith("//"))
    assert "listening on ws://" not in code


@pytest.mark.parametrize("name", ["servers.yaml", "gateway.env.template"])
def test_the_seed_files_exist_for_the_bundle_to_carry(name: str) -> None:
    assert (TAURI / "seed" / name).is_file()


def test_the_window_may_rename_itself_and_nothing_more() -> None:
    """One permission beyond `core:default`, and it is the white-labelling one.

    The window's title is baked into `tauri.conf.json` at build time; the deployment's own
    title lives in a config file the daemon reads at run time. The page is the only thing
    that sees both, so `setTitle` over IPC is how they meet -- see
    `src/mcp_gateway/branding.md`.

    Pinned as an exact list because the interesting failure is not this permission going
    missing, it is the next one arriving beside it. `core:default` is broad already; every
    addition after it is a decision, and a decision in a JSON array is invisible in review.
    """
    capability = json.loads((TAURI / "capabilities" / "default.json").read_text())
    assert capability["permissions"] == ["core:default", "core:window:allow-set-title"]
    assert capability["windows"] == ["main"]
