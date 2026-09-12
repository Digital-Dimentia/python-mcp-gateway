"""What the desktop shell depends on, asserted from this side of the boundary.

`desktop/` is Rust, and nothing in this repo's Python test suite runs it. So every place
the shell reaches into the daemon is a coupling that a Python change could break silently:
the build would stay green, `cargo test` would stay green, and the failure would surface as
a window that never connects on somebody's machine.

The four couplings are pinned here. Each one is load-bearing in a specific way:

* **The startup line.** `supervisor.rs` learns the port by reading the daemon's own
  `listening on ws://host:port/mcp` off stderr. That makes a log line a wire format. The
  alternative -- a `--port-file` flag, or an inherited socket -- is Python surface added for
  one consumer, and is filed rather than guessed at; until then, this test is what makes the
  existing line safe to depend on.
* **A headerless client with a Bearer token is accepted.** `proxy.rs` dials with
  `tokio-tungstenite`, which sends no `Origin`. `origin_permitted(None, ...)` returning True
  is what lets the whole shell exist without touching `transport_ws.py` -- so it is no
  longer merely a convenience for non-browser clients, and tightening it would break the
  desktop app.
* **The environment beats the file.** The shell mints a key per launch and passes it in the
  environment. A user who later pastes something into `gateway.env` must not be able to
  take the sockets away from the app that spawned it.
* **The seeded config loads.** It is copied into the app data directory on first launch; if
  it does not parse, the daemon exits 2 before binding and the first thing a new user sees
  is a window that never connects.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets

from mcp_gateway import transport_ws
from mcp_gateway.cli import DEFAULT_PORT
from mcp_gateway.config import load as load_config
from mcp_gateway.secrets import SecretStore
from tests.fixtures.ws_client import daemon

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED = REPO_ROOT / "desktop" / "src-tauri" / "seed"

#: How long to wait for a daemon with no backends to bind. Generous: a cold import on a
#: loaded CI box is the slow part, and a flake here reads as a broken contract.
STARTUP_TIMEOUT = 30.0


def parse_listening(line: str) -> int | None:
    """The port out of the daemon's startup line, or None.

    **This is a twin of `parse_listening` in `desktop/src-tauri/src/supervisor.rs`.** Keep
    the two together: if one is taught something, teach the other.

    A substring search rather than a line anchor, because the default log format is a bare
    `%(message)s` but `--debug` prefixes the logger name -- and a shell that only worked
    without `--debug` would break exactly when someone turned logging up to find out why.
    `rsplit` on the colon, so an IPv6 authority like `[::1]:8765` still yields the port.
    """
    _, separator, rest = line.partition("listening on ws://")
    if not separator:
        return None
    authority = rest.split("/", 1)[0]
    _, colon, port = authority.rpartition(":")
    if not colon:
        return None
    try:
        return int(port.strip())
    except ValueError:
        return None


def run_until_listening(tmp_path: Path, *extra: str) -> tuple[int, list[str]]:
    """Start a real daemon as a subprocess and read its port off stderr, as Rust does."""
    config = tmp_path / "servers.yaml"
    config.write_text("version: 1\nservers: {}\n")

    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_gateway.cli", "--port", "0", "--config", str(config), *extra],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )
    lines: list[str] = []
    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        assert process.stderr is not None
        while time.monotonic() < deadline:
            line = process.stderr.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            port = parse_listening(line)
            if port is not None:
                return port, lines
        pytest.fail(f"no startup line within {STARTUP_TIMEOUT}s; saw:\n" + "\n".join(lines))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - a daemon that will not go
            process.kill()
            process.wait(timeout=10)
        if process.stderr is not None:
            process.stderr.close()
    raise AssertionError("unreachable")        # pragma: no cover


# --- the startup line ---------------------------------------------------------------------


def test_the_startup_line_carries_a_real_port(tmp_path) -> None:
    """The port the shell dials comes from here and nowhere else."""
    port, lines = run_until_listening(tmp_path)
    assert 1 <= port <= 65535, lines
    # Not the default. `--port 0` has to mean "the OS picks" -- the shell relies on that
    # so a running `make run` daemon on 8765 and the desktop app can coexist.
    assert port != DEFAULT_PORT, lines


def test_the_startup_line_survives_debug_logging(tmp_path) -> None:
    """`--debug` prefixes the logger name, which is why the parser does not anchor."""
    port, lines = run_until_listening(tmp_path, "--debug")
    assert 1 <= port <= 65535, lines
    marker = next(line for line in lines if "listening on ws://" in line)
    assert "transport_ws" in marker, f"expected a logger-name prefix under --debug: {marker!r}"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("listening on ws://127.0.0.1:49613/mcp", 49613),
        ("mcp_gateway.transport_ws: listening on ws://127.0.0.1:8765/mcp", 8765),
        ("listening on ws://[::1]:8765/mcp", 8765),
        ("listening on ws://localhost:1/mcp", 1),
        # Things that must not be mistaken for it.
        ("server listening on 127.0.0.1:49613", None),
        ("admin UI at http://127.0.0.1:49613/ui/", None),
        ("listening on ws://127.0.0.1/mcp", None),
        ("listening on ws://127.0.0.1:notaport/mcp", None),
        ("", None),
    ],
)
def test_the_parser_agrees_with_its_rust_twin(line: str, expected: int | None) -> None:
    assert parse_listening(line) == expected


def test_the_daemon_still_spells_the_line_the_way_the_parser_reads_it() -> None:
    """Guard the format at its source, so an edit to `transport_ws` is caught here too."""
    source = (REPO_ROOT / "src" / "mcp_gateway" / "transport_ws.py").read_text()
    assert re.search(r'"listening on ws://%s:%s%s"', source), (
        "the startup line changed shape; desktop/src-tauri/src/supervisor.rs parses it"
    )


# --- the proxy's client ---------------------------------------------------------------------


async def test_a_bearer_client_with_no_origin_is_accepted(tmp_path) -> None:
    """Exactly the request `proxy.rs` makes. `tokio-tungstenite` sends no `Origin`."""
    key = "desktop-shell-key"
    harness = await daemon(tmp_path, access_key=key)
    try:
        socket = await websockets.connect(
            harness.url("/admin", key=None),
            additional_headers={"Authorization": f"Bearer {key}"},
        )
        await socket.close()
    finally:
        await harness.close()


async def test_the_same_client_without_the_key_is_refused(tmp_path) -> None:
    """The other half: no `Origin` is not itself a way in."""
    harness = await daemon(tmp_path, access_key="desktop-shell-key")
    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
            await websockets.connect(harness.url("/admin", key=None))
        assert caught.value.response.status_code == 401
    finally:
        await harness.close()


# --- where the key comes from ---------------------------------------------------------------


def test_the_environment_beats_the_credential_file() -> None:
    """The shell's key wins over anything a user later writes into `gateway.env`.

    Otherwise a `WS_ACCESS_KEY` pasted into the seeded file would take the sockets away from
    the app that spawned the daemon, and the window would sit there unable to connect to its
    own child.
    """
    store = SecretStore({transport_ws.ACCESS_KEY_SECRET_NAME: "from-the-file"})
    resolved = transport_ws.resolve_access_key(
        store, environ={transport_ws.ACCESS_KEY_ENV: "from-the-shell"}
    )
    assert resolved == "from-the-shell"


# --- the seeded files -------------------------------------------------------------------------


def test_the_seeded_config_loads() -> None:
    """Copied into the app data directory on first launch, so it has to parse."""
    config = load_config(SEED / "servers.yaml")
    assert config.servers == {}


def test_the_seeded_env_template_names_no_access_key() -> None:
    """The shell always overrides it, so a line here would be read, ignored and confusing."""
    text = (SEED / "gateway.env.template").read_text()
    assert not re.search(r"^\s*(export\s+)?WS_ACCESS_KEY=", text, re.MULTILINE)


def test_the_seeded_env_template_is_not_itself_a_credential_store() -> None:
    """Named `.template` so `--check` beside the seeded config does not read it as one."""
    assert not (SEED / "gateway.env").exists()
