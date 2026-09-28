"""Start a daemon for a bridge that found nothing listening, and take it away again.

`mcp-gateway-connect` dials a daemon; it does not manage one. That is the whole of
[`bridge.md`](bridge.md)'s argument for why the bridge is correct for every revision of MCP
forever, and this module is the one deliberate exception to it -- kept in its own file so
that the exception is visible rather than buried in a pump.

## Why it exists

A stdio-only IDE spawns one process and nothing else. There is no second place to put "and
also run the daemon", so without this the daemon has to be alive by some other arrangement --
`make run`, a launchd job, a systemd unit -- and if it is not, the bridge retries forever
while the IDE shows a server that never answers and the reason sits on a stderr the IDE may
not surface.

## Why the port is the lock

Two IDEs each spawn a bridge, both find nothing listening, and both start a daemon. Only one
can bind, and the loser exits on `address already in use` -- so the race needs no lockfile, no
pidfile and no portfile to arbitrate it. The bridge that lost simply connects to the daemon
that won on its next retry, because the retry loop was already there. That is also why this
takes a host and a port rather than discovering them: the URL already named them.

`portfile.py` is for the opposite problem -- `--port 0`, where nobody knows the port until the
socket exists. Here the port is an input, so there is nothing to discover.

## What it refuses to start

Only a plaintext loopback daemon, and only when it was told which config to use.

* **Loopback only.** `wss://gateway.lan:8765` names a daemon on another machine; starting a
  local one would bind a port nobody asked about and then fail to be the thing the URL meant.
* **`ws://` only.** Serving TLS needs a certificate and a key this has no way to choose, so a
  `wss://` URL is someone else's daemon by construction.
* **An explicit port.** `ws://127.0.0.1/mcp` means port 80, and binding that needs root; a URL
  with no port is much more likely a mistake than a request.
* **An explicit config.** The daemon's own default is `./servers.yaml`, and a bridge is
  spawned from whatever directory the client happened to be in -- so inheriting that default
  would start a daemon against a config nobody chose, or fail in a directory that has none.
  `bridge.md` makes the same argument about guessing at `./gateway.env`.

Each refusal logs what to add instead, because a feature that silently does nothing is worse
than one that is not there.

## Why SIGTERM to the process, not the group

The daemon has its own signal handler and its own shutdown ladder for the backends it owns
(see [`cli.md`](cli.md) and [`mcp_stdio.md`](mcp_stdio.md)). Signalling the process lets it
run that; signalling the group would reach the backends directly and around it, which is the
one thing the daemon's teardown exists to do properly.

The child is deliberately **not** put in a new session. Sharing the bridge's process group
means that whatever kills the bridge -- including a `SIGKILL` this code never gets to react
to -- takes the daemon with it, so the promise that nothing is left running that nobody
started holds even when this module does not get the chance to keep it.

## Why stdout goes to the void

The bridge's stdout is the protocol wire. A child that inherited it could desynchronise the
client with a single stray byte, so it gets `DEVNULL` -- and nothing is lost, because
`configure_logging` sends every daemon diagnostic to stderr, which the child *does* inherit
so that the IDE's log shows why a daemon did not start.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from mcp_gateway.transport_ws import ACCESS_KEY_ENV

logger = logging.getLogger(__name__)

#: Ask for autostart without a flag, for a client whose config takes an environment but not
#: an argument list. Any value but these means yes, so `MCP_GATEWAY_AUTOSTART=1` reads the
#: way anyone would expect.
AUTOSTART_ENV = "MCP_GATEWAY_AUTOSTART"
_FALSEY = frozenset({"", "0", "false", "no", "off"})

#: Hosts a daemon started here could actually be. Not a security boundary -- the daemon has
#: its own -- but the difference between "start one" and "someone else's machine".
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

#: How long the daemon gets to shut its backends down after SIGTERM before SIGKILL. The
#: daemon's own ladder is shorter than this, so reaching the timeout means it is wedged.
_STOP_TIMEOUT = 5.0


def requested(flag: bool, environ: dict[str, str] | None = None) -> bool:
    """Whether autostart was asked for, by flag or by environment."""
    if flag:
        return True
    source = os.environ if environ is None else environ
    return source.get(AUTOSTART_ENV, "").strip().lower() not in _FALSEY


def target(url: str) -> tuple[str, int] | None:
    """`(host, port)` a daemon could be started on for `url`, or `None` with a reason logged.

    See the module docstring for why each of these is a refusal rather than a default.
    """
    parts = urlsplit(url)
    if parts.scheme != "ws":
        logger.warning(
            "autostart: %s is not a ws:// URL, so it names a daemon this cannot start "
            "(serving TLS needs a certificate); start it yourself, or drop --autostart",
            url,
        )
        return None
    host = parts.hostname
    if host is None or host.lower() not in _LOOPBACK:
        logger.warning(
            "autostart: %s is not loopback, so it names a daemon on another machine; "
            "start it there, or drop --autostart",
            url,
        )
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        logger.warning(
            "autostart: %s names no port, and the implied port 80 is not something to bind "
            "on a guess; give the URL the daemon's port",
            url,
        )
        return None
    return host, port


class Daemon:
    """One `mcp-gateway` this bridge started, and is therefore responsible for."""

    def __init__(self, host: str, port: int, config: Path, key: str | None) -> None:
        self.host = host
        self.port = port
        self.config = config
        self.key = key
        self._process: subprocess.Popen[bytes] | None = None

    def command(self) -> list[str]:
        """`python -m mcp_gateway.cli`, with *this* interpreter.

        `sys.executable` rather than the `mcp-gateway` console script for the same reason an
        IDE's config needs the absolute path to `mcp-gateway-connect`: the venv is not
        necessarily on `PATH`, and the interpreter running this is by definition the one the
        gateway is installed into. `python -m mcp_gateway.cli` is the invocation
        `scripts/start-gateway.sh` already uses.
        """
        return [
            sys.executable,
            "-m",
            "mcp_gateway.cli",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--config",
            str(self.config),
        ]

    def environment(self) -> dict[str, str]:
        """The child's environment: ours, plus the key we resolved if we found one.

        Handed over in the environment and never in argv, which is world-readable through
        `ps` -- the same rule `make run` follows. With no key, nothing is set and the daemon
        runs keyless, which is what a bridge holding no key needs it to do if the two are to
        agree about whether there is one.
        """
        environ = dict(os.environ)
        if self.key:
            environ[ACCESS_KEY_ENV] = self.key
        return environ

    def start(self) -> bool:
        """Spawn a daemon, once. `True` if this call started one.

        Only ever once per bridge. A daemon that exits immediately is usually the one that
        lost the bind race, and respawning into that would be a fork bomb wearing a retry
        loop; a daemon that exits for any other reason has said why on the stderr it
        inherited, and saying it repeatedly would not help.
        """
        if self._process is not None:
            return False
        command = self.command()
        logger.info("autostart: nothing on %s:%d; starting a daemon", self.host, self.port)
        logger.debug("autostart: %s", " ".join(command))
        try:
            self._process = subprocess.Popen(  # noqa: S603 -- argv built here, no shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                env=self.environment(),
            )
        except OSError as exc:
            logger.error("autostart: could not start a daemon: %s", exc)
            return False
        return True

    def exited(self) -> int | None:
        """The child's status if it is gone, else `None`. Never blocks."""
        return None if self._process is None else self._process.poll()

    def stop(self) -> None:
        """Terminate the daemon we started, if it is still running. Never raises.

        Called on every exit path the bridge has. Safe to call twice, and safe to call when
        nothing was ever started, so no caller has to know which of those it is.
        """
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is not None:
            logger.debug("autostart: the daemon we started is already gone")
            return
        logger.info("autostart: stopping the daemon we started")
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("autostart: daemon did not stop in %.0fs; killing it", _STOP_TIMEOUT)
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_STOP_TIMEOUT)
