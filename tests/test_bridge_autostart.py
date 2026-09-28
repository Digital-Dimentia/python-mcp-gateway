"""`--autostart`: the bridge starting the daemon it dials, and stopping it again.

The end-to-end cases spawn a real `mcp-gateway-connect` against a port with nothing on it,
because what is being claimed is about processes: that one appears, that it is the daemon the
URL named, and that it is gone afterwards. Asserting that from inside the process that would
have spawned it proves nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import sys
from pathlib import Path

import pytest

from mcp_gateway import autostart
from mcp_gateway.bridge import build_parser, resolve_autostart
from mcp_gateway.transport_ws import ACCESS_KEY_ENV

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"

SERVERS = f"""
servers:
  docs:
    command: {sys.executable}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "docs"
      MOCK_TOOLS: "echo"
"""


def _free_port() -> int:
    """A port nothing is listening on. Racy in principle; the OS does not hand it out twice."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def _listening(port: int, *, timeout: float) -> bool:
    """Whether something accepts a TCP connection on `port` within `timeout`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    return False


async def _gone(port: int, *, timeout: float) -> bool:
    """Whether `port` stops accepting connections within `timeout`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return True
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await asyncio.sleep(0.1)
    return False


# --- what it refuses to start ------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "because"),
    [
        ("wss://127.0.0.1:8765/mcp", "serving TLS needs a certificate nothing here can pick"),
        ("ws://gateway.lan:8765/mcp", "that is a daemon on another machine"),
        ("ws://192.168.1.10:8765/mcp", "still not loopback"),
        ("ws://127.0.0.1/mcp", "port 80 is not something to bind on a guess"),
    ],
)
def test_a_url_that_names_someone_elses_daemon_is_refused(url: str, because: str) -> None:
    assert autostart.target(url) is None, because


@pytest.mark.parametrize(
    "url", ["ws://127.0.0.1:8765/mcp", "ws://localhost:8765/mcp", "ws://[::1]:8765/mcp"]
)
def test_a_loopback_url_names_a_daemon_that_can_be_started(url: str) -> None:
    where = autostart.target(url)
    assert where is not None
    assert where[1] == 8765


def test_autostart_is_off_unless_asked_for() -> None:
    assert not autostart.requested(False, {})
    assert autostart.requested(True, {})


@pytest.mark.parametrize("value", ["1", "yes", "true", "anything"])
def test_the_environment_can_ask_for_it(value: str) -> None:
    """An IDE whose config takes an environment but not an argument list."""
    assert autostart.requested(False, {autostart.AUTOSTART_ENV: value})


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "  OFF  "])
def test_the_environment_can_decline_it(value: str) -> None:
    assert not autostart.requested(False, {autostart.AUTOSTART_ENV: value})


def test_no_config_means_no_autostart(caplog) -> None:
    """The daemon's `./servers.yaml` default is not inherited: see `autostart.md`.

    A bridge runs in whatever directory its client chose, so that default names a file nobody
    picked -- the same argument `bridge.md` makes about guessing at `./gateway.env`.
    """
    args = _parse("--autostart", "--url", "ws://127.0.0.1:8765/mcp")
    with caplog.at_level("WARNING"):
        assert resolve_autostart(args, "ws://127.0.0.1:8765/mcp", None, {}) is None
    assert "--config" in caplog.text


def test_the_config_can_come_from_the_environment() -> None:
    args = _parse("--autostart", "--url", "ws://127.0.0.1:8765/mcp")
    daemon = resolve_autostart(
        args, "ws://127.0.0.1:8765/mcp", None, {"MCP_GATEWAY_CONFIG": "/tmp/servers.yaml"}
    )
    assert daemon is not None
    assert daemon.config == Path("/tmp/servers.yaml")


def test_not_asking_for_it_starts_nothing() -> None:
    args = _parse("--url", "ws://127.0.0.1:8765/mcp", "--config", "/tmp/servers.yaml")
    assert resolve_autostart(args, "ws://127.0.0.1:8765/mcp", None, {}) is None


# --- how the daemon is invoked -----------------------------------------------------------


def test_the_daemon_runs_on_this_interpreter_not_whatever_is_on_path() -> None:
    """The venv is not necessarily on an IDE's PATH; `sys.executable` always is the right one."""
    command = autostart.Daemon("127.0.0.1", 9, Path("/tmp/servers.yaml"), None).command()
    assert command[:3] == [sys.executable, "-m", "mcp_gateway.cli"]
    assert "--port" in command and "9" in command
    assert "--config" in command and "/tmp/servers.yaml" in command


def test_the_key_goes_in_the_environment_and_never_in_argv() -> None:
    """argv is world-readable through `ps`, which is the whole reason `run` exports it too."""
    daemon = autostart.Daemon("127.0.0.1", 9, Path("/tmp/servers.yaml"), "s3cret")
    assert "s3cret" not in " ".join(daemon.command())
    assert daemon.environment()[ACCESS_KEY_ENV] == "s3cret"


def test_no_key_means_the_daemon_is_started_keyless() -> None:
    """Both sides have to agree there is no key, or the bridge cannot authenticate to it."""
    daemon = autostart.Daemon("127.0.0.1", 9, Path("/tmp/servers.yaml"), None)
    assert ACCESS_KEY_ENV not in daemon.environment()


def test_stopping_something_that_was_never_started_is_not_an_error() -> None:
    autostart.Daemon("127.0.0.1", 9, Path("/tmp/servers.yaml"), None).stop()


# --- end to end --------------------------------------------------------------------------


async def _bridge(tmp_path: Path, port: int, *extra: str):
    """A real `mcp-gateway-connect`, with stderr on a file so a test can read the log."""
    log = tmp_path / "bridge.stderr"
    handle = log.open("wb")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_gateway.bridge",
        "--url",
        f"ws://127.0.0.1:{port}/mcp",
        *extra,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=handle,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return proc, log, handle


async def _request(proc, id_: int, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    payload = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    proc.stdin.write((json.dumps(payload) + "\n").encode())
    await proc.stdin.drain()
    line = await asyncio.wait_for(proc.stdout.readline(), timeout)
    assert line, "bridge closed stdout"
    return json.loads(line)


async def _handshake(proc) -> dict:
    """`initialize` plus the notification the daemon waits for before it answers anything else."""
    reply = await _request(
        proc,
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "autostart-test", "version": "0"},
        },
    )
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await proc.stdin.drain()
    return reply


async def test_an_ide_with_no_daemon_running_gets_a_working_gateway(tmp_path) -> None:
    """The whole point: one process in the IDE's config, and the fan-out comes up behind it.

    `initialize` is answered by the daemon, not by the bridge -- the bridge answers nothing on
    its own -- so a reply here is proof a daemon was started, bound, and reached.
    """
    config = tmp_path / "servers.yaml"
    config.write_text(SERVERS, encoding="utf-8")
    port = _free_port()
    assert not await _listening(port, timeout=0.3), "the test's own port was already busy"

    proc, log, handle = await _bridge(tmp_path, port, "--autostart", "--config", str(config))
    try:
        reply = await _handshake(proc)
        assert reply["result"]["serverInfo"]["name"] == "mcp-gateway"

        tools = await _request(proc, 2, "tools/list")
        names = [tool["name"] for tool in tools["result"]["tools"]]
        assert any(name.startswith("docs__") for name in names), names
        assert "autostart" in log.read_text(encoding="utf-8")
    finally:
        proc.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), 15)
        if proc.returncode is None:  # pragma: no cover - only if the bridge wedges
            proc.kill()
            await proc.wait()
        handle.close()

    assert await _gone(port, timeout=15), "the daemon outlived the bridge that started it"


async def test_a_daemon_that_is_already_running_is_left_alone(tmp_path) -> None:
    """`--autostart` is a no-op when the connect succeeds, which is why it spawns *after* one.

    Started by hand here, the way a launchd job or `make run` would have, and the bridge must
    attach to that one rather than racing a second daemon onto the same port.
    """
    config = tmp_path / "servers.yaml"
    config.write_text(SERVERS, encoding="utf-8")
    port = _free_port()
    existing = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_gateway.cli",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--config",
        str(config),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await _listening(port, timeout=30), "the hand-started daemon never bound"

    proc, log, handle = await _bridge(tmp_path, port, "--autostart", "--config", str(config))
    try:
        reply = await _handshake(proc)
        assert reply["result"]["serverInfo"]["name"] == "mcp-gateway"
        assert "starting a daemon" not in log.read_text(encoding="utf-8")
    finally:
        proc.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), 15)
        if proc.returncode is None:  # pragma: no cover
            proc.kill()
            await proc.wait()
        handle.close()

    # Still serving: the bridge stops only a daemon it started itself.
    assert await _listening(port, timeout=2), "the bridge stopped a daemon it did not start"
    existing.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(existing.wait(), 15)
    if existing.returncode is None:  # pragma: no cover
        existing.kill()
        await existing.wait()
