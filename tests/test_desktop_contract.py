"""What the desktop shell depends on, asserted from this side of the boundary.

`desktop/` is Rust, and nothing in this repo's Python test suite runs it. So every place
the shell reaches into the daemon is a coupling that a Python change could break silently:
the build would stay green, `cargo test` would stay green, and the failure would surface as
a window that never connects on somebody's machine.

The four couplings are pinned here. Each one is load-bearing in a specific way:

* **The port file.** `supervisor.rs` learns the port by polling the file `--port-file`
  names. Three things have to hold and each is pinned below: the flag exists, the file
  appears with the bound port in it once the socket is up, and it is gone again after the
  daemon exits. This used to be a scrape of the `listening on ws://host:port/mcp` log line,
  which worked but made a sentence written for a human into a wire format nobody could
  reword; see python-mcp-gateway-9p3.
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

import contextlib
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import websockets

from mcp_gateway import portfile, transport_ws
from mcp_gateway.cli import DEFAULT_PORT
from mcp_gateway.config import load as load_config
from mcp_gateway.secrets import SecretStore
from tests.fixtures.ws_client import daemon

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED = REPO_ROOT / "desktop" / "src-tauri" / "seed"

#: How long to wait for a daemon with no backends to bind. Generous: a cold import on a
#: loaded CI box is the slow part, and a flake here reads as a broken contract.
STARTUP_TIMEOUT = 30.0


@contextlib.contextmanager
def daemon_with_port_file(tmp_path: Path, *extra: str):
    """A real daemon as a subprocess, yielding `(port, path)` while it is still running.

    A context manager rather than a function returning the port, because half of what is
    being pinned here is about the daemon being *alive* -- that the port in the file is one
    you can actually connect to -- and half is about it being gone, which the caller checks
    after the block. A helper that terminated the child before returning could test neither.
    """
    config = tmp_path / "servers.yaml"
    config.write_text("version: 1\nservers: {}\n")
    port_file = tmp_path / "gateway.port"

    process = subprocess.Popen(
        [
            sys.executable, "-m", "mcp_gateway.cli",
            "--port", "0",
            "--port-file", str(port_file),
            "--config", str(config),
            *extra,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        port = None
        while time.monotonic() < deadline:
            port = portfile.read(port_file)
            if port is not None:
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if port is None:
            process.terminate()
            said = process.stderr.read() if process.stderr else ""
            pytest.fail(f"no port file within {STARTUP_TIMEOUT}s; the daemon said:\n{said}")
        yield port, port_file
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - a daemon that will not go
            process.kill()
            process.wait(timeout=10)
        if process.stderr is not None:
            process.stderr.close()


# --- the port file ---------------------------------------------------------------------------


def test_the_flag_exists_and_is_spelled_the_way_the_shell_spells_it() -> None:
    """`supervisor.argv` passes `--port-file`. A rename here is a shell that never connects."""
    help_text = subprocess.run(
        [sys.executable, "-m", "mcp_gateway.cli", "--help"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    ).stdout
    assert "--port-file" in help_text


def test_the_port_file_carries_a_real_port(tmp_path) -> None:
    """The port the shell dials comes from here and nowhere else."""
    with daemon_with_port_file(tmp_path) as (port, _):
        assert 1 <= port <= 65535
        # Not the default. `--port 0` has to mean "the OS picks" -- the shell relies on that
        # so a running `make run` daemon on 8765 and the desktop app can coexist.
        assert port != DEFAULT_PORT


def test_the_port_file_names_the_port_actually_bound(tmp_path) -> None:
    """Not a number it hoped for. A file written before the bind would send the shell's
    sockets somewhere nothing is listening."""
    with daemon_with_port_file(tmp_path) as (port, _):
        with socket.socket() as probe:
            probe.settimeout(5)
            probe.connect(("127.0.0.1", port))


def test_the_port_file_is_removed_when_the_daemon_exits(tmp_path) -> None:
    """A stale file names a port something else may hold by now, which is worse than none."""
    with daemon_with_port_file(tmp_path) as (_, path):
        assert path.exists()
    # Outside the block the child has been terminated and reaped.
    for _ in range(100):
        if not path.exists():
            break
        time.sleep(0.05)
    assert not path.exists()


def test_the_port_file_survives_debug_logging(tmp_path) -> None:
    """The old scrape broke under `--debug`, which prefixes the logger name. A file cannot
    have that problem, and pinning it says the coupling really did move off the log."""
    with daemon_with_port_file(tmp_path, "--debug") as (port, _):
        assert 1 <= port <= 65535


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("49613\n", 49613),
        ("8765", 8765),
        ("  8765  \n", 8765),
        # Things a poller can genuinely read, none of which is a port.
        ("", None),
        ("   ", None),
        ("notaport", None),
        ("8765x", None),
        ("-1", None),
        ("0", None),
        ("65536", None),
    ],
)
def test_the_reader_agrees_with_its_rust_twin(contents: str, expected: int | None, tmp_path) -> None:
    """**Twinned with `parse_port_file` in `desktop/src-tauri/src/supervisor.rs`.** Keep the
    two together: if one is taught something, teach the other."""
    path = tmp_path / "gateway.port"
    path.write_text(contents)
    assert portfile.read(path) == expected


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
