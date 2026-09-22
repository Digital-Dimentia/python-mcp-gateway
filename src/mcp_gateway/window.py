"""A desktop window around a gateway running in this same process.

The second desktop host, beside the Tauri shell in `src/desktop/`. That one is better --
it bundles its own interpreter, so it installs onto a machine with no Python at all -- but
it is built with `cargo`, and there are managed environments where Rust is unapproved
software and the crate downloads are blocked outright. This one needs nothing but a
`pip install`.

It is small because almost none of the Rust host's work turned out to be about being a
desktop app. The supervisor, the process-group teardown and the WebSocket proxy that keeps
the access key out of the page are all consequences of the daemon being a *different*
process from the window. Here it is the same process, and all three stop existing: there
is no child to orphan, and a page loaded from `http://127.0.0.1:<port>/ui/` is same-origin
with the sockets it dials, so `browserTransport` in `rpc.js` -- the path a browser has
always used -- is the path the window uses too.

What is left is the two things a window genuinely needs: something to serve it, and a way
to stop. See `window.md` for what was deliberately not ported.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_gateway import __version__, webui
from mcp_gateway.cli import (
    DEFAULT_HOST,
    EXIT_REFUSED,
    build_gateway,
    configure_logging,
    default_config_path,
    default_env_path,
)
from mcp_gateway.config import ConfigError
from mcp_gateway.gateway import Gateway
from mcp_gateway.secrets import SecretError
from mcp_gateway.transport_ws import UnauthenticatedBindError, unauthenticated_bind_allowed

logger = logging.getLogger(__name__)

#: The extra that carries the toolkit. Named in the refusal rather than described, so the
#: line can be pasted.
DESKTOP_EXTRA = "pip install 'python-mcp-gateway[desktop]'"

#: What to do when pywebview is installed but has no engine to render into. Per platform,
#: because the answer is completely different on each and a generic sentence would help
#: nobody.
#:
#: pywebview declares its macOS and Windows bindings itself -- pyobjc under
#: `sys_platform == "darwin"`, pythonnet under `"win32"` -- so `pip` alone is enough on
#: both, and what can still be missing is a *system* component. On Linux it declares
#: nothing: the bindings are its own `gtk` and `qt` extras, so a plain install succeeds and
#: then has nothing to draw with. That asymmetry is the whole reason this dictionary exists.
#: See `window.md`.
NO_ENGINE_HELP = {
    "linux": (
        "pywebview needs a GUI backend, which it does not install on Linux. Either "
        "`pip install 'pywebview[qt]'` (binary wheels, no system packages) or "
        "`pip install 'pywebview[gtk]'` plus the distribution's gobject-introspection and "
        "WebKit2GTK packages (on Debian/Ubuntu: libgirepository1.0-dev gir1.2-webkit2-4.1)."
    ),
    "win32": (
        "pywebview needs the WebView2 runtime, which ships with Edge and is present on "
        "Windows 11 and most Windows 10. Install the Evergreen Runtime from Microsoft if "
        "this machine is missing it."
    ),
    "darwin": (
        "pywebview needs PyObjC, which it installs itself on macOS -- so this usually means "
        "a broken install: try `pip install --force-reinstall pywebview`."
    ),
}

#: How long `serve_in_background` waits for the socket. Generous: the backends start before
#: the bind, and a cold `npx` backend on a slow disk is the reason this is not five seconds.
READY_TIMEOUT = 60.0

#: How long `Session.stop` waits for the backends to drain before giving up on the thread.
#: The loop thread is a daemon thread, so giving up costs an ugly exit, not a hang.
STOP_TIMEOUT = 20.0

#: The window, in the same numbers as `src/desktop/src-tauri/tauri.conf.json`. Not a fresh
#: guess: the admin UI's own `@media (max-width: 60rem)` fallback is the narrow layout, and
#: the size at which the wide one is worth having was settled once for the Tauri shell. The
#: two hosts opening at different sizes would be a difference with no reason behind it.
#:
#: pywebview's default is 800x600, which lands under that breakpoint and shows the UI
#: stacked and clipped -- the reason these are here at all.
WINDOW_SIZE = (1280, 820)
#: Below this the UI is legible but cramped; it is where the Tauri shell stops shrinking.
WINDOW_MIN_SIZE = (720, 480)


class WindowError(RuntimeError):
    """The window could not be opened. Always a refusal the user can act on."""


@dataclass(frozen=True)
class Session:
    """A gateway serving on a background thread, and the window's half of it.

    Frozen and small on purpose: everything the GUI side needs is a string or an int read
    once, so the window code never touches the gateway or its loop except through `stop`.
    """

    #: The bound port. Never 0 -- this is read after `start`, from `server.port`.
    port: int
    #: What to navigate to, key included when there is one. See `webui.url`.
    url: str
    #: What to put on the title bar: the same name the log prints, from `servers.yaml`.
    title: str

    _gateway: Gateway
    _loop: asyncio.AbstractEventLoop
    _thread: threading.Thread

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Ask the gateway to shut down and wait for the backends to be reaped.

        Idempotent, because the window can close in more than one way -- the close button,
        Ctrl+C in the terminal that launched it, an exception on the way up -- and every
        one of them runs this.
        """
        if not self._thread.is_alive():
            return
        # `shutdown_requested` belongs to the other thread's loop, and `asyncio.Event` is
        # not thread-safe: `set` has to happen *in* that loop, not merely soon.
        self._loop.call_soon_threadsafe(self._gateway.shutdown_requested.set)
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("the gateway did not stop within %.0fs; exiting anyway", timeout)


async def _serve_until_stopped(
    config_path: Path,
    env_path: Path,
    host: str,
    port: int,
    box: dict[str, Any],
    ready: threading.Event,
) -> None:
    """The whole of the background thread's life. Mirrors `cli._serve` minus TLS, the port
    file and the signal handlers -- a window is not a daemon and has none of those callers.

    `box` and `ready` are how the bind result crosses back to the main thread: either a
    port and a gateway, or the exception that stopped it. `ready` is set on *both* paths,
    or a refusal would show as a timeout.
    """
    try:
        gateway, access_key = build_gateway(config_path, env_path, host=host, port=port)
        await gateway.start(
            access_key=access_key,
            allow_unauthenticated=unauthenticated_bind_allowed(),
        )
    except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread by `_ready`
        box["error"] = exc
        ready.set()
        return

    box["gateway"] = gateway
    box["loop"] = asyncio.get_running_loop()
    box["port"] = gateway.server.port
    box["key"] = access_key
    box["title"] = gateway.config.branding.title
    ready.set()

    try:
        serving = asyncio.create_task(gateway.serve_forever())
        stopping = asyncio.create_task(gateway.shutdown_requested.wait())
        await asyncio.wait([serving, stopping], return_when=asyncio.FIRST_COMPLETED)
        for task in (serving, stopping):
            task.cancel()
    finally:
        # `finally` and not a context manager, for the reason `cli._serve` gives: the one
        # thing that must happen on every exit path is that the backends are reaped.
        logger.info("shutting down; stopping backends")
        await gateway.stop()


def serve_in_background(
    config_path: Path,
    env_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = 0,
    timeout: float = READY_TIMEOUT,
) -> Session:
    """Start a gateway on its own thread and return once it is bound, or raise.

    Knows nothing about pywebview -- deliberately. This is the half of the host with
    something to get wrong, and it is exercised headless by `tests/test_window.py`; the
    GUI half below is a dozen lines with no decisions in them.

    `port=0` by default. A window is not something anyone connects to from outside, so
    nothing needs to guess the number, and an ephemeral port is what lets a second window
    open while the first is still up.
    """
    ready = threading.Event()
    box: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: asyncio.run(
            _serve_until_stopped(config_path, env_path, host, port, box, ready)
        ),
        # A daemon thread: if the main thread dies in a way that skips `stop`, the process
        # should still exit. The backends are reaped by their own supervisor on the way
        # down, and an exit that leaks one is better than one that never happens.
        daemon=True,
        name="mcp-gateway",
    )
    thread.start()

    if not ready.wait(timeout):
        raise WindowError(f"the gateway did not bind within {timeout:.0f}s")
    error = box.get("error")
    if error is not None:
        # Raised on *this* thread, so the caller's `except ConfigError` works exactly as it
        # does for the daemon. The traceback from the other thread is not interesting: every
        # error that reaches here is a refusal whose message is the whole story.
        raise error

    return Session(
        port=box["port"],
        url=webui.url(host, box["port"], box["key"]),
        title=box["title"],
        _gateway=box["gateway"],
        _loop=box["loop"],
        _thread=thread,
    )


def build_parser() -> argparse.ArgumentParser:
    """The window's command line: the daemon's, minus everything a window cannot use.

    No `--host` (a window dials loopback or nothing), no TLS (there is no network hop to
    protect), no `--port-file` (nothing is supervising this). `--port` survives only
    because someone may need a fixed one to point a separate client at.
    """
    parser = argparse.ArgumentParser(
        prog="mcp-gateway-desktop",
        description="Open the gateway's admin UI in a desktop window.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to servers.yaml")
    parser.add_argument("--env", type=Path, default=None, help="path to gateway.env")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="bind this port instead of an ephemeral one",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging on stderr")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _import_webview() -> Any:
    """`pywebview`, or a refusal naming the extra.

    Imported here and not at module scope, which is the point of the whole arrangement:
    `pip install python-mcp-gateway` must not acquire a GUI toolkit, and `webui.py` and the
    daemon must stay importable on a machine that has no display at all.
    """
    try:
        import webview  # noqa: PLC0415 -- see the docstring
    except ImportError as missing:
        raise WindowError(
            f"the desktop window needs pywebview, which is not installed: {DESKTOP_EXTRA}"
        ) from missing
    return webview


def run() -> None:
    """Console-script entrypoint (`mcp-gateway-desktop`).

    Refusals become `SystemExit(2)` and print one line, exactly as the daemon's `run` does:
    a misconfigured `servers.yaml` is the same mistake whichever host found it, and a window
    that opens onto an error page would be a worse way to say so than a terminal line.
    """
    args = build_parser().parse_args()
    configure_logging(args.debug)
    config_path = args.config if args.config is not None else default_config_path()
    env_path = args.env if args.env is not None else default_env_path(config_path)

    try:
        webview = _import_webview()
        session = serve_in_background(config_path, env_path, port=args.port)
    except (ConfigError, SecretError, UnauthenticatedBindError, WindowError) as refusal:
        logger.error("%s", refusal)
        raise SystemExit(EXIT_REFUSED) from None

    logger.info("serving %s", session.url)
    refused = False
    try:
        width, height = WINDOW_SIZE
        webview.create_window(
            session.title,
            session.url,
            width=width,
            height=height,
            min_size=WINDOW_MIN_SIZE,
        )
        # Blocks until the last window closes. pywebview must own the main thread on every
        # platform it supports, which is why the gateway is the thing that moved.
        webview.start()
    except KeyboardInterrupt:
        # Ctrl+C in the terminal that launched the window. Swallowed for the reason the
        # daemon swallows it: it is a way of asking to stop, not a fault.
        pass
    except webview.WebViewException as no_engine:
        # pywebview finds its rendering engine lazily, so "installed but nothing to draw
        # with" surfaces here rather than at import -- after the backends have started. The
        # `finally` below still reaps them; what this adds is the sentence that says which
        # package is missing, since pywebview's own message names every toolkit it supports
        # rather than the one this platform wants.
        logger.error("%s", no_engine)
        logger.error("%s", NO_ENGINE_HELP.get(sys.platform, f"see {DESKTOP_EXTRA}"))
        refused = True
    finally:
        session.stop()
    if refused:
        raise SystemExit(EXIT_REFUSED)


if __name__ == "__main__":
    # `python -m mcp_gateway.window`, which is what `make run-window` invokes. Without this
    # the module would define everything and exit 0 with no window and no error -- see the
    # same guard in `cli.py` for the failure that taught this.
    run()
