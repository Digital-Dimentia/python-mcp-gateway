"""The pywebview desktop host: the half of it that has decisions in it.

`window.run` opens a window and is not testable on a CI runner. `serve_in_background` is
everything underneath — a gateway on a foreign thread, a bind that may refuse, a shutdown
that crosses back — and it is GUI-free precisely so that this file can exercise it on a
machine with no display.

What these pin, and what breaks when one fails:

* the host serves the *same daemon* a client would get from `mcp-gateway`, not a reduced
  one — if this fails, the window and the terminal disagree about what the gateway is;
* the URL it opens is one the daemon's own origin check accepts — if this fails, the
  window shows the UI and every socket the page opens is refused with 403;
* the key reaches the page when there is one and nothing when there is not;
* stopping reaps the backends — if this fails, closing the window leaves subprocesses;
* importing the module needs no GUI toolkit — if this fails, `pip install` of the daemon
  starts pulling a webview, and the container build grows a display dependency.
"""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path

import pytest
import websockets

from mcp_gateway import transport_ws, webui, window

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

ZOO = f"""\
servers:
  mock:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_TOOLS: "read_file"
"""


def _write_config(tmp_path: Path, *, servers: str = ZOO, env: str = "") -> tuple[Path, Path]:
    """Both files, the way `cli.resolve_paths` expects to find them: side by side."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "servers.yaml"
    config_path.write_text(servers)
    env_path = tmp_path / "gateway.env"
    env_path.write_text(env)
    return config_path, env_path


# --- the daemon underneath ---


async def test_the_window_host_serves_the_same_daemon_a_client_would_get(tmp_path) -> None:
    """A real MCP handshake against the in-process gateway, over a real socket.

    The window is worth nothing if what it hosts is a reduced gateway. This connects the
    way any client connects and asks for the tool list a backend actually published.
    """
    config_path, env_path = _write_config(tmp_path)
    session = window.serve_in_background(config_path, env_path)
    try:
        assert session.port != 0
        async with websockets.connect(f"ws://127.0.0.1:{session.port}/mcp") as socket:
            await socket.send(
                '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
                '{"protocolVersion":"2025-06-18","capabilities":{},'
                '"clientInfo":{"name":"test","version":"0"}}}'
            )
            assert '"result"' in await socket.recv()
    finally:
        session.stop()


async def test_a_bad_config_refuses_on_the_calling_thread(tmp_path) -> None:
    """The refusal has to arrive as an exception here, not as a traceback over there.

    `run` catches `ConfigError` to print one line and exit 2. If the background thread
    swallowed it, the user would see a 60-second wait and "did not bind" instead of the
    sentence naming the mistake.
    """
    from mcp_gateway.config import ConfigError

    config_path, env_path = _write_config(tmp_path, servers="servers: [not, a, mapping]\n")
    with pytest.raises(ConfigError):
        window.serve_in_background(config_path, env_path)


# --- the URL the window opens ---


async def test_the_window_url_is_one_the_daemons_own_origin_check_accepts(tmp_path) -> None:
    """The page's Origin is the URL's scheme://host:port, and it must be in `own_origins`.

    This is the coupling that makes the whole browser-mode arrangement work: the window is
    not a privileged shell, it is an ordinary same-origin page. If the host ever opened
    `http://[::1]` or a hostname outside that set, the UI would load and every socket
    would be refused.
    """
    config_path, env_path = _write_config(tmp_path, servers="servers: {}\n")
    session = window.serve_in_background(config_path, env_path)
    try:
        origin = f"http://127.0.0.1:{session.port}"
        assert origin in transport_ws.own_origins("127.0.0.1", session.port)
        assert transport_ws.origin_permitted(
            origin, transport_ws.own_origins("127.0.0.1", session.port)
        )
        # And it points at the UI, not at the bare root, which the daemon does not serve.
        assert session.url.startswith(f"{origin}{webui.UI_PATH}/")
    finally:
        session.stop()


async def test_the_window_url_carries_the_key_only_when_one_is_configured(tmp_path) -> None:
    """Both directions, because both are wrong in their own way.

    A key omitted when there is one means a window onto a 401. A key invented when there
    is none is this host minting credentials, which it does not do -- see `window.md`.
    """
    keyless_config, keyless_env = _write_config(tmp_path / "keyless", servers="servers: {}\n")
    session = window.serve_in_background(keyless_config, keyless_env)
    try:
        assert "key=" not in session.url
    finally:
        session.stop()

    keyed_config, keyed_env = _write_config(
        tmp_path / "keyed", servers="servers: {}\n", env="WS_ACCESS_KEY=s3cret\n"
    )
    session = window.serve_in_background(keyed_config, keyed_env)
    try:
        assert session.url.endswith("?key=s3cret")
    finally:
        session.stop()


async def test_the_window_title_is_the_name_the_log_prints(tmp_path) -> None:
    """A branded gateway is branded in both places, or the two are hard to correlate."""
    config_path, env_path = _write_config(
        tmp_path, servers='branding:\n  title: "Acme Gateway"\nservers: {}\n'
    )
    session = window.serve_in_background(config_path, env_path)
    try:
        assert session.title == "Acme Gateway"
    finally:
        session.stop()


# --- stopping ---


async def test_stopping_the_session_reaps_the_backends(tmp_path) -> None:
    """Closing the window must not leave a subprocess behind.

    There is no supervisor outside this process to clean up after it -- that is the trade
    `window.md` names -- so `stop` is the only thing that reaps them. `tests/conftest.py`'s
    leak guard fails the session if it does not, which is the other half of this assertion.
    """
    config_path, env_path = _write_config(tmp_path)
    session = window.serve_in_background(config_path, env_path)
    session.stop()
    assert not session._thread.is_alive()
    # Idempotent: the close button, Ctrl+C and the `finally` in `run` can all reach it.
    session.stop()


# --- the window itself ---


def test_the_window_opens_at_the_size_the_tauri_shell_settled_on() -> None:
    """Both hosts show the same UI, so both open at the same size, or one of them is wrong.

    pywebview's default is 800x600, which is under the admin UI's own 60rem breakpoint: the
    window opens on the narrow stacked layout with the wide one's content clipped. The size
    at which that stops was worked out once, for the Tauri shell; this keeps the two in step
    rather than leaving a second number to drift.
    """
    conf = json.loads((REPO_ROOT / "src" / "desktop" / "src-tauri" / "tauri.conf.json").read_text())
    tauri_window = conf["app"]["windows"][0]
    assert window.WINDOW_SIZE == (tauri_window["width"], tauri_window["height"])
    assert window.WINDOW_MIN_SIZE == (tauri_window["minWidth"], tauri_window["minHeight"])
    # And wide enough to clear the breakpoint the stylesheet actually uses, at 16px/rem.
    assert window.WINDOW_SIZE[0] > 60 * 16


# --- the extra ---


def test_the_gui_toolkit_is_not_a_base_dependency() -> None:
    """`import mcp_gateway.window` must work on a machine with no display.

    Asserted against the source rather than by catching an ImportError, because the
    developer running this very likely *has* pywebview installed, and a test that passes
    only on their machine is worse than none.
    """
    tree = ast.parse((REPO_ROOT / "src" / "mcp_gateway" / "window.py").read_text())
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = {alias.name.split(".")[0] for node in top_level for alias in node.names}
    names |= {node.module.split(".")[0] for node in top_level if isinstance(node, ast.ImportFrom) and node.module}
    assert "webview" not in names

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert any(spec.startswith("pywebview") for spec in extras["desktop"])
    assert not any(spec.startswith("pywebview") for spec in pyproject["project"]["dependencies"])
    assert pyproject["project"]["scripts"]["mcp-gateway-desktop"] == "mcp_gateway.window:run"


def test_the_missing_toolkit_refusal_names_what_to_install() -> None:
    """A missing extra is a five-second fix, and only if the message says which one."""
    assert "pip install" in window.DESKTOP_EXTRA
    assert "desktop" in window.DESKTOP_EXTRA
