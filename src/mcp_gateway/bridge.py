"""`mcp-gateway-connect`: pump newline-delimited JSON between stdin/stdout and the daemon.

Claude Code and Claude Desktop attach an MCP server over stdio. The daemon speaks
WebSocket. This is the ~200 lines that let today's clients reach it unchanged, and it is
deliberately the *only* place that bridges them -- the daemon stays one protocol on one
port.

## It does not parse MCP

It reads a line, checks it is JSON, and sends it as a frame; it reads a frame and writes it
as a line. It never inspects `method`, never tracks an id, and never answers anything on its
own except the one case below.

That is not laziness, it is the correctness argument. A bridge that understood MCP would
have a version of the protocol baked into it, and would silently drop or mangle anything a
newer revision added. This one is correct for every revision, forever, because it has no
opinion.

## stdout is the wire

The one module in this project where that is true. Nothing may be printed; `sys.stdout` is
redirected to stderr for the life of the process so that a stray `print` -- ours or a
library's -- lands somewhere harmless instead of desynchronising the client.

## Reconnecting rather than exiting

A daemon restart is routine: an operator upgrades it, launchd cycles it, a reload goes
wrong. If the bridge exited, `claude mcp` would mark the server dead and the human would
have to notice and re-add it.

So a dropped connection is retried with backoff, and any request that was in flight when the
socket died is answered with an error naming the reason -- because a client waiting forever
on a request the daemon never saw is worse than a request that failed. That is the one case
this module answers on its own, and it needs the id, which is the one field it reads.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any

import websockets

from mcp_gateway import __version__, errors, jsonrpc
from mcp_gateway.cli import configure_logging, default_config_path, default_env_path
from mcp_gateway.transport_ws import ACCESS_KEY_ENV, ACCESS_KEY_SECRET_NAME, MAX_MESSAGE_BYTES

logger = logging.getLogger(__name__)

#: Where to dial when nothing says otherwise. Matches the daemon's own defaults so the
#: zero-configuration case works.
DEFAULT_URL = "ws://127.0.0.1:8765/mcp"
URL_ENV = "MCP_GATEWAY_URL"

#: A CA bundle to verify a `wss://` daemon against, for a certificate the system store does
#: not already trust -- a private CA, or a self-signed certificate that is its own.
CA_FILE_ENV = "MCP_GATEWAY_TLS_CA"

#: Reconnect backoff. Starts fast because the overwhelmingly common cause is a daemon
#: restarting, which takes under a second; caps low enough that a human waiting on it does
#: not conclude the thing is broken.
_BACKOFF_INITIAL = 0.25
_BACKOFF_MAX = 5.0
_BACKOFF_FACTOR = 2.0

#: How long a message read from stdin waits for a connection before being failed.
#:
#: A client writes `initialize` the instant it spawns us, which is routinely before the
#: socket is up -- so a message arriving with no connection is the *normal* first case, not
#: an error. It is only an error if the daemon stays unreachable, and this is how long we
#: give it. Comfortably more than the backoff cap, so a reconnect in progress is waited out
#: rather than failed.
_CONNECT_WAIT_SECONDS = 15.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-gateway-connect",
        description=(
            "Bridge an stdio MCP client to a running mcp-gateway daemon. Reads JSON-RPC on "
            "stdin, forwards it over WebSocket, and writes the daemon's replies to stdout."
        ),
    )
    parser.add_argument("--version", action="version", version=f"mcp-gateway-connect {__version__}")
    parser.add_argument(
        "--url",
        default=None,
        help=f"Daemon endpoint (default: ${URL_ENV}, else {DEFAULT_URL}).",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help=(
            f"Credential store to read {ACCESS_KEY_SECRET_NAME} from, when "
            f"${ACCESS_KEY_ENV} is unset."
        ),
    )
    parser.add_argument(
        "--ca-file",
        type=Path,
        metavar="PEM",
        default=None,
        help=(
            f"Verify a wss:// daemon against this CA bundle instead of the system store "
            f"(default: ${CA_FILE_ENV}). Verification is never turned off."
        ),
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Exit when the daemon goes away instead of retrying. For scripts and tests.",
    )
    parser.add_argument("--debug", action="store_true", help="Log at DEBUG, to stderr.")
    return parser


def resolve_url(args: argparse.Namespace, environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    return args.url or source.get(URL_ENV) or DEFAULT_URL


def resolve_ca_file(args: argparse.Namespace, environ: dict[str, str] | None = None) -> Path | None:
    source = os.environ if environ is None else environ
    configured = args.ca_file or source.get(CA_FILE_ENV)
    return Path(configured) if configured else None


def client_tls(url: str, ca_file: Path | None) -> ssl.SSLContext | None:
    """The context to dial `url` with, or `None` to let `websockets` choose.

    `None` for `ws://` whatever was configured, because `websockets` refuses an `ssl`
    argument on a plaintext URI, and for `wss://` with no CA file, because its default is
    already the system store with verification on. Built once, here, so a missing or
    unreadable bundle fails at startup rather than on every reconnect.
    """
    if ca_file is None or not url.startswith("wss://"):
        return None
    return ssl.create_default_context(cafile=str(ca_file))


def resolve_key(args: argparse.Namespace, environ: dict[str, str] | None = None) -> str | None:
    """The access key, from the environment first and a credential store second.

    Reads `gateway.env` only when told where it is, or when it sits beside a config file the
    environment names. A bridge is usually spawned from an arbitrary directory by a client,
    so guessing `./gateway.env` would be guessing.
    """
    source = os.environ if environ is None else environ
    from_env = source.get(ACCESS_KEY_ENV)
    if from_env:
        return from_env

    env_path = args.env
    if env_path is None and (source.get("MCP_GATEWAY_ENV") or source.get("MCP_GATEWAY_CONFIG")):
        env_path = default_env_path(default_config_path(source), source)
    if env_path is None:
        return None

    from mcp_gateway.secrets import SecretError
    from mcp_gateway.secrets import load as load_secrets

    try:
        return load_secrets(Path(env_path)).get(ACCESS_KEY_SECRET_NAME) or None
    except SecretError as exc:
        logger.warning("could not read %s: %s", env_path, exc)
        return None


def connect_kwargs(key: str | None, tls: ssl.SSLContext | None = None) -> dict[str, Any]:
    """`Authorization: Bearer` rather than `?key=`.

    Both are accepted by the daemon, and the bridge is the one client we control, so it uses
    the carrier that does not end up in an access log. See `transport_ws.md`.
    """
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    kwargs: dict[str, Any] = {"additional_headers": headers, "max_size": MAX_MESSAGE_BYTES}
    if tls is not None:
        kwargs["ssl"] = tls
    return kwargs


class Bridge:
    """One stdin/stdout pair, pumped to a daemon that may come and go."""

    def __init__(
        self,
        url: str,
        key: str | None,
        *,
        reconnect: bool = True,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.url = url
        self.key = key
        self.tls = tls
        self.reconnect = reconnect
        self._websocket: Any = None
        #: Ids that were **actually sent** to the daemon and not yet answered. If the socket
        #: dies, each gets an error rather than silence -- a client waiting forever is worse
        #: than a request that failed.
        #:
        #: A request still queued in the stdin pump is deliberately *not* in here. It has
        #: not been sent, so a reconnect can still deliver it, and failing it on every retry
        #: cycle would make the bridge give up on work it was about to do.
        self._in_flight: set[Any] = set()
        self._stdout = sys.stdout
        #: Set while a socket is up. The stdin pump waits on it rather than checking for
        #: `None`, so the ordinary "client spoke before we finished connecting" case is a
        #: short wait instead of a spurious failure.
        self._connected = asyncio.Event()

    # --- stdout is the wire -------------------------------------------------------------

    def write(self, message: dict[str, Any]) -> None:
        self._stdout.write(jsonrpc.encode(message) + "\n")
        self._stdout.flush()

    def fail_one(self, request_id: Any, reason: str) -> None:
        """Answer a single request that could not be sent. Notifications are dropped.

        A notification has no id and no reply, so there is nothing to answer and nothing the
        client is waiting on -- inventing an error for one would put an unsolicited response
        on a wire that never asked.
        """
        if request_id is None:
            return
        self._in_flight.discard(request_id)
        self.write(
            jsonrpc.failure(
                request_id,
                errors.error_object(
                    errors.INTERNAL_ERROR,
                    f"mcp-gateway-connect could not reach the daemon: {reason}",
                    {"bridge": True, "url": self.url},
                ),
            )
        )

    def fail_in_flight(self, reason: str) -> None:
        for request_id in sorted(self._in_flight, key=str):
            self.write(
                jsonrpc.failure(
                    request_id,
                    errors.error_object(
                        errors.INTERNAL_ERROR,
                        f"mcp-gateway-connect lost its connection to the daemon: {reason}",
                        {"bridge": True, "url": self.url},
                    ),
                )
            )
        self._in_flight.clear()

    # --- the two pumps ------------------------------------------------------------------

    async def _stdin_to_socket(self, stdin: asyncio.StreamReader) -> None:
        while True:
            line = await stdin.readline()
            if not line:
                logger.info("client closed stdin; exiting")
                raise _ClientHungUp()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = jsonrpc.decode(text)
            except Exception:
                # Never forwarded: the daemon would answer with a parse error carrying a
                # null id, which tells the client nothing it did not already know. Answer
                # here instead, where the offending line is in hand.
                logger.debug("dropping unparseable line from client")
                self.write(
                    jsonrpc.failure(
                        None, errors.error_object(errors.PARSE_ERROR, "invalid JSON from client")
                    )
                )
                continue
            request_id = jsonrpc.request_id_of(message)
            try:
                await asyncio.wait_for(self._connected.wait(), _CONNECT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                self.fail_one(request_id, f"no connection to {self.url}")
                continue
            websocket = self._websocket
            if websocket is None:  # lost between the wait and here
                self.fail_one(request_id, "connection dropped")
                continue
            try:
                await websocket.send(text)
            except Exception as exc:
                self.fail_one(request_id, str(exc) or exc.__class__.__name__)
                continue
            # Only now is it the daemon's problem, and only now can losing the socket mean
            # this request will never be answered.
            if request_id is not None:
                self._in_flight.add(request_id)

    async def _socket_to_stdout(self, websocket: Any) -> None:
        async for raw in websocket:
            try:
                message = jsonrpc.decode(raw)
            except Exception:
                logger.warning("dropping unparseable frame from daemon")
                continue
            request_id = jsonrpc.request_id_of(message)
            if request_id is not None:
                self._in_flight.discard(request_id)
            self.write(message)

    # --- the loop -----------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        backoff = _BACKOFF_INITIAL
        while True:
            try:
                async with websockets.connect(self.url, **connect_kwargs(self.key, self.tls)) as websocket:
                    logger.info("connected to %s", self.url)
                    backoff = _BACKOFF_INITIAL
                    self._websocket = websocket
                    self._connected.set()
                    await self._socket_to_stdout(websocket)
                reason = "daemon closed the connection"
            except Exception as exc:
                reason = str(exc) or exc.__class__.__name__
            finally:
                self._websocket = None
                self._connected.clear()

            self.fail_in_flight(reason)
            if not self.reconnect:
                logger.info("not reconnecting (--no-reconnect): %s", reason)
                return
            logger.info("disconnected (%s); retrying in %.2fs", reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    async def run(self) -> None:
        """Pump both directions until the client hangs up or the daemon is given up on.

        The two are peers, awaited together: the stdin side ends on EOF, the socket side
        ends when reconnection is off and the daemon is gone. Whichever finishes first ends
        the bridge -- which is why the pump cannot be a fire-and-forget task, as an
        exception raised inside one nobody awaits is a bridge that hangs instead of exiting.
        """
        stdin = await _stdin_reader()
        pump = asyncio.create_task(self._stdin_to_socket(stdin), name="stdin")
        socket = asyncio.create_task(self._connect_loop(), name="socket")
        done, pending = await asyncio.wait({pump, socket}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*pending)
        for task in done:
            with contextlib.suppress(_ClientHungUp, asyncio.CancelledError):
                task.result()


class _ClientHungUp(Exception):
    """stdin reached EOF: the client is gone, so the bridge has nothing left to do."""


async def _stdin_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    return reader


def _reserve_stdout() -> None:
    """Point `sys.stdout` at stderr, keeping the real one for the wire.

    POSIX only. Windows is excluded because some transports resolve `sys.stdout` at write
    time, which would send the protocol to stderr instead -- the exact inversion this is
    meant to prevent.
    """
    if os.name != "posix":
        return
    sys.stdout = sys.stderr


def run() -> None:
    """Console-script entrypoint for `mcp-gateway-connect`."""
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.debug)

    url = resolve_url(args)
    key = resolve_key(args)
    try:
        tls = client_tls(url, resolve_ca_file(args))
    except (OSError, ssl.SSLError) as exc:
        logger.error("cannot use CA bundle %s: %s", resolve_ca_file(args), exc)
        raise SystemExit(2) from None
    bridge = Bridge(url, key, reconnect=not args.no_reconnect, tls=tls)
    # After the Bridge captured the real stdout, and before anything else can print.
    _reserve_stdout()

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # `python -m mcp_gateway.bridge`, which `make connect` uses
    run()
