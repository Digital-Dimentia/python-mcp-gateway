"""Parse the command line, configure logging, and run the daemon.

The only module that touches `sys.argv` or raises `SystemExit`. Everything below it takes
its configuration as arguments, which is what lets the whole gateway be driven from a
test without a process boundary.

## Paths resolve against the config file, not the cwd

`--env` defaults to a sibling of the *resolved* `--config` path rather than to
`./gateway.env`. A daemon is started by launchd, by a supervisor, or by a shell in some
arbitrary directory -- launchd in particular starts it in `/` -- and resolving the
credential file against the caller's cwd would make "one gitignored `gateway.env` at the
repo root" work in development and silently fail in the one deployment that matters.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Callable

from mcp_gateway import __version__
from mcp_gateway.branding import Branding
from mcp_gateway.config import ConfigError, GatewayConfig, ServerSpec, load as load_config
from mcp_gateway.gateway import Gateway
from mcp_gateway.logging_redaction import install_redaction
from mcp_gateway import portfile
from mcp_gateway.secret_providers import FILE_ORIGIN, build_store
from mcp_gateway.secrets import MissingSecret, SecretError, SecretStore, missing_for
from mcp_gateway.transport_ws import (
    TLS_CERT_ENV,
    TLS_KEY_ENV,
    TlsError,
    UnauthenticatedBindError,
    resolve_access_key,
    tls_context,
    unauthenticated_bind_allowed,
)

logger = logging.getLogger(__name__)

#: argparse's own code for "you asked for something I will not do". Reused for every
#: startup refusal so a supervisor sees one number for the whole class.
EXIT_REFUSED = 2

#: Bound before the socket is, and printed in `--list`. 8765 is what the README, the
#: container examples and the Makefile banner all advertise.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Overrides for the two file paths, for a deployment that cannot pass flags -- a
#: container ENTRYPOINT, a launchd job whose ProgramArguments are awkward to edit.
CONFIG_ENV = "MCP_GATEWAY_CONFIG"
ENV_FILE_ENV = "MCP_GATEWAY_ENV"

DEFAULT_CONFIG_NAME = "servers.yaml"
DEFAULT_ENV_NAME = "gateway.env"

# One `%(message)s` normally, the logger name prepended under --debug. Two formats rather
# than one with an empty field, because the common case is read by a human watching a
# terminal and a bare message is what that wants.
_LOG_FORMAT = "%(message)s"
_DEBUG_LOG_FORMAT = "%(name)s: %(message)s"


def configure_logging(debug: bool) -> None:
    """Send logging to **stderr**, at DEBUG when asked.

    stderr and not stdout even though this process's stdout is not a protocol wire: the
    same `configure_logging` is called by `mcp_gateway.bridge`, where it is, and a format
    decision that is correct in one entrypoint and corrupting in the other is not a
    decision worth having twice.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format=_DEBUG_LOG_FORMAT if debug else _LOG_FORMAT,
        stream=sys.stderr,
    )


def default_config_path(environ: dict[str, str] | None = None) -> Path:
    """Where `--config` points when it is not given.

    `environ` is a parameter, defaulting to `os.environ`, so a test can exercise the
    precedence without monkeypatching the process it runs in.
    """
    source = os.environ if environ is None else environ
    configured = source.get(CONFIG_ENV)
    if configured:
        return Path(configured)
    return Path.cwd() / DEFAULT_CONFIG_NAME


def default_env_path(config_path: Path, environ: dict[str, str] | None = None) -> Path:
    """Where `--env` points when it is not given: beside the resolved config file.

    See the module docstring. The explicit `MCP_GATEWAY_ENV` still wins, for the
    deployment that keeps its credentials somewhere else entirely -- a tmpfs, a mounted
    secret -- while the config stays in the checkout.
    """
    source = os.environ if environ is None else environ
    configured = source.get(ENV_FILE_ENV)
    if configured:
        return Path(configured)
    return config_path.resolve().parent / DEFAULT_ENV_NAME


def build_parser() -> argparse.ArgumentParser:
    """The command line. Split out so a test can assert defaults without invoking `run`."""
    parser = argparse.ArgumentParser(
        prog="mcp-gateway",
        description=(
            "An MCP gateway daemon. Serves MCP over WebSocket to any number of clients "
            "and fans out to the stdio MCP servers named in servers.yaml, injecting each "
            "one's credentials from gateway.env."
        ),
    )
    parser.add_argument("--version", action="version", version=f"mcp-gateway {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            f"Path to the server catalogue (default: ${CONFIG_ENV}, else "
            f"./{DEFAULT_CONFIG_NAME}). YAML or JSON -- a YAML 1.2 safe loader reads both."
        ),
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help=(
            f"Path to the credential store (default: ${ENV_FILE_ENV}, else "
            f"{DEFAULT_ENV_NAME} beside the resolved --config)."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Interface to bind (default: {DEFAULT_HOST}). Binding anything but loopback "
            "without an access key is refused; see MCP_GATEWAY_WS_KEY."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})."
    )
    parser.add_argument(
        "--tls-cert",
        type=Path,
        metavar="PEM",
        default=os.environ.get(TLS_CERT_ENV) or None,
        help=(
            f"Serve wss:// and https:// with this certificate chain (default: ${TLS_CERT_ENV}). "
            "Without it the port is plaintext, which off loopback is only right behind a "
            "TLS-terminating proxy."
        ),
    )
    parser.add_argument(
        "--tls-key",
        type=Path,
        metavar="PEM",
        default=os.environ.get(TLS_KEY_ENV) or None,
        help=(
            f"The certificate's private key (default: ${TLS_KEY_ENV}). May be omitted when "
            "the --tls-cert file holds the key as well."
        ),
    )
    parser.add_argument(
        "--port-file",
        type=Path,
        metavar="PATH",
        help=(
            "Write the bound port to PATH once the socket is listening, and remove it on "
            "exit. The way to learn the port chosen by --port 0 without scraping a log line."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Validate the config and credential store, report every problem found, and "
            "exit 0 or 2. Binds no port and spawns no backend."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_plan",
        help=(
            "Print the resolved spawn plan to stderr and exit: each backend's command, "
            "args, cwd, and the NAMES of the environment variables it will receive. "
            "Values are never printed."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log at DEBUG, including every MCP message in both directions.",
    )
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """The config and credential paths this invocation will use."""
    config_path = args.config if args.config is not None else default_config_path()
    env_path = args.env if args.env is not None else default_env_path(config_path)
    return config_path, env_path


def _resolve_missing(config: GatewayConfig, store: SecretStore) -> dict[str, list[MissingSecret]]:
    """Every unresolvable `${VAR}`, per enabled server.

    The implementation is `secrets.missing_for`, shared with `admin.secrets.missing` so the
    UI and `--check` cannot disagree about what "missing" means. Kept as a name here because
    that is what `check()` reads as.
    """
    return missing_for(config, store)


def _describe(spec: ServerSpec) -> str:
    """One backend's resolved spawn plan, as a human reads it.

    Env **key names only**, never values -- this is printed by `--list`, which exists
    precisely so an operator can check the plan without opening the credential store.
    """
    lines = [f"  {spec.name}:"]
    lines.append(f"    command: {spec.command} {' '.join(spec.args)}".rstrip())
    if spec.cwd:
        lines.append(f"    cwd: {spec.cwd}")
    lines.append(f"    env_mode: {spec.env_mode}")
    if spec.env:
        lines.append(f"    env keys: {', '.join(spec.env_keys)}")
    if spec.env_passthrough:
        lines.append(f"    env_passthrough: {', '.join(spec.env_passthrough)}")
    flags = [f"timeout={spec.timeout:g}s", f"startup_timeout={spec.startup_timeout:g}s"]
    if spec.required:
        flags.append("required")
    lines.append(f"    {', '.join(flags)}")
    return "\n".join(lines)


def _print_branding(branding: Branding) -> None:
    """What a white-labelled deployment calls itself, reported by `--check` and `--list`.

    The icon is named rather than described: a broken path is already a `ConfigError` by the
    time this runs, so what is worth printing is *which* file is being served, which is the
    question someone has when the logo on screen is not the one they expected.
    """
    print(
        f"brand: {branding.title} (serverInfo name: {branding.name})",
        file=sys.stderr,
    )
    if branding.icon_path is not None:
        print(f"icon: {branding.icon_path}", file=sys.stderr)


def _describe_sources(config: GatewayConfig, env_path: Path, store: SecretStore) -> str:
    """The `secrets:` line of `--check`, naming every source and its key count.

    One line rather than one per source: the interesting number is how many keys resolved,
    and an operator with no providers configured -- which is most of them -- must not have
    to read past a section header to find it.
    """
    counts: dict[str, int] = {}
    for key in store.keys():
        origin = store.origin(key) or FILE_ORIGIN
        counts[origin] = counts.get(origin, 0) + 1
    if not config.secret_providers:
        return f"{env_path} ({len(store.keys())} key(s))"
    parts = [f"{spec.ref} ({counts.get(spec.ref, 0)} key(s))" for spec in config.secret_providers]
    parts.append(f"{env_path} ({counts.get(FILE_ORIGIN, 0)} key(s))")
    return " -> ".join(parts)


def check(
    config_path: Path,
    env_path: Path,
    *,
    list_plan: bool = False,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> int:
    """Validate both files without binding a port or spawning anything.

    Returns the process exit code. Everything goes to **stderr**, including the plan: the
    two files this reports on are the ones an operator pipes around, and a report on stdout
    invites `mcp-gateway --list > servers.yaml`.
    """
    try:
        config = load_config(config_path)
    except ConfigError as refusal:
        print(str(refusal), file=sys.stderr)
        return EXIT_REFUSED
    try:
        # Runs any configured provider, which is the point: `--check` exists to fail on
        # this machine before the daemon does, and an unreachable Vault is exactly the
        # kind of thing it should catch.
        store = build_store(config, env_path)
    except SecretError as refusal:
        print(str(refusal), file=sys.stderr)
        return EXIT_REFUSED

    # Install redaction before anything below can echo a value by accident. The two loads
    # above never log one -- `secrets.load` logs paths and modes only, asserted by test.
    install_redaction(store)

    # Loaded, not just stat'ed: a key that does not match its certificate is the mistake
    # that actually happens, and only `load_cert_chain` catches it.
    try:
        tls = tls_context(tls_cert, tls_key)
    except TlsError as refusal:
        print(str(refusal), file=sys.stderr)
        return EXIT_REFUSED

    print(f"config: {config_path}", file=sys.stderr)
    print(f"secrets: {_describe_sources(config, env_path, store)}", file=sys.stderr)
    # Only when on, like the brand line below.
    if tls is not None:
        print(f"tls: {tls_cert}", file=sys.stderr)
    # Only when there is one. An unbranded gateway printing "brand: MCP Gateway" is a line
    # that carries no information and has to be read past every time.
    if config.branding.customised:
        _print_branding(config.branding)

    enabled = config.enabled
    disabled = [n for n, s in config.servers.items() if not s.enabled]
    print(f"{len(enabled)} enabled server(s), {len(disabled)} disabled", file=sys.stderr)

    if list_plan:
        for spec in enabled.values():
            print(_describe(spec), file=sys.stderr)

    problems = _resolve_missing(config, store)
    fatal = False
    for name, missing in problems.items():
        required = config.servers[name].required
        for problem in missing:
            print(
                f"{'ERROR' if required else 'WARN '} {name}: {problem}",
                file=sys.stderr,
            )
        if required:
            fatal = True

    if not problems:
        print("all enabled servers have their credentials", file=sys.stderr)
    elif not fatal:
        print(
            f"{len(problems)} server(s) would be skipped at startup; the rest would serve",
            file=sys.stderr,
        )
    return EXIT_REFUSED if fatal else 0


def _install_signal_handlers(gateway: Gateway) -> None:
    """SIGHUP reloads; SIGTERM and SIGINT shut down cleanly.

    SIGHUP is the **primary operator interface** for a daemon, not an afterthought: someone
    rotating a credential may have no client attached to call `gateway__reload_config`. The
    template declines SIGHUP for its stdio transport, reasonably -- there the process is the
    client's child and restarting it is trivial. This one is long-lived and shared.
    """
    loop = asyncio.get_running_loop()

    def _reload() -> None:
        logger.info("SIGHUP: reloading configuration")
        loop.create_task(gateway.reload())

    if hasattr(signal, "SIGHUP"):  # not on Windows
        _handle(loop, signal.SIGHUP, _reload)

    stop = asyncio.Event()
    # SIGBREAK is Windows' Ctrl-Break, and the only console signal there that is not SIGINT.
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            _handle(loop, sig, stop.set)
    gateway.shutdown_requested = stop


def _handle(loop: asyncio.AbstractEventLoop, sig: int, action: Callable[[], None]) -> None:
    """Run `action` in the loop when `sig` arrives, by whichever mechanism this OS has.

    `loop.add_signal_handler` is a Unix method. asyncio's Windows loops raise
    `NotImplementedError` from it, and because handlers are installed *before* the socket
    is bound, that exception was not a missing convenience -- it was the bundled daemon
    failing to start at all, reported to the desktop shell as `no port file`.

    Windows gets `signal.signal` instead. That runs the handler in the interpreter's main
    thread rather than in the loop, so what it does is wake the loop through
    `call_soon_threadsafe` rather than touch the `Event` from under it.
    """
    try:
        loop.add_signal_handler(sig, action)
        return
    except NotImplementedError:
        pass
    try:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(action))
    except ValueError:
        # `signal.signal` is main-thread only, and the daemon is embeddable: a caller
        # running it in a worker thread gets no handlers rather than no daemon.
        logger.debug("no handler for signal %s: not the main thread", sig)


async def _serve(args: argparse.Namespace, config_path: Path, env_path: Path) -> None:
    """Load, start backends, bind, and serve until told to stop."""
    config = load_config(config_path)
    store = build_store(config, env_path)
    # The access key goes in even when it came from the environment rather than the store:
    # `websockets` logs the request line, query string and all, at DEBUG.
    access_key = resolve_access_key(store)
    install_redaction(store, extra=[access_key] if access_key else [])
    # Before the backends start, so a bad certificate costs nothing to find out about.
    tls = tls_context(args.tls_cert, args.tls_key)

    # Before the bind, so the name in the log is the name on the window when someone
    # correlates the two.
    if config.branding.customised:
        logger.info(
            "serving as %r (serverInfo name %r)", config.branding.title, config.branding.name
        )

    gateway = Gateway(
        config,
        store,
        config_path=config_path,
        env_path=env_path,
        host=args.host,
        port=args.port,
    )
    _install_signal_handlers(gateway)

    await gateway.start(
        access_key=access_key,
        allow_unauthenticated=unauthenticated_bind_allowed(),
        tls=tls,
    )
    # After `start`, never before: with `--port 0` the port does not exist until the socket
    # is bound, and a file appearing with the wrong number in it is worse than no file. Its
    # appearance is therefore also the readiness signal, which is the other half of what a
    # supervisor wants and the reason this is not simply logged.
    port_file = portfile.write(args.port_file, gateway.server.port) if args.port_file else None
    # `finally` and not a context manager: the one thing that must happen on every exit
    # path is that the backend subprocesses are reaped, and a daemon exits by signal far
    # more often than by falling off the end of a block.
    try:
        serving = asyncio.create_task(gateway.serve_forever())
        stopping = asyncio.create_task(gateway.shutdown_requested.wait())
        await asyncio.wait([serving, stopping], return_when=asyncio.FIRST_COMPLETED)
        for task in (serving, stopping):
            task.cancel()
    finally:
        logger.info("shutting down; stopping backends")
        # Before the backends, which take a moment to drain: the file says "this daemon is
        # serving on this port", and it stops being true the instant we begin shutting down.
        portfile.remove(port_file)
        await gateway.stop()


def run() -> None:
    """Console-script entrypoint.

    `KeyboardInterrupt` is swallowed rather than allowed to print a traceback: Ctrl+C is
    how a foreground daemon is meant to be stopped, and a stack trace says otherwise.

    The startup refusals become `SystemExit(2)` -- argparse's own code for "you asked
    for something I will not do" -- because each is configuration the operator must fix,
    and none ever reaches a client.
    """
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.debug)
    config_path, env_path = resolve_paths(args)

    if args.check or args.list_plan:
        raise SystemExit(
            check(
                config_path,
                env_path,
                list_plan=args.list_plan,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
            )
        )

    try:
        asyncio.run(_serve(args, config_path, env_path))
    except (ConfigError, SecretError) as refusal:
        logger.error("%s", refusal)
        raise SystemExit(EXIT_REFUSED) from None
    except (UnauthenticatedBindError, TlsError) as refusal:
        logger.error("%s", refusal)
        raise SystemExit(EXIT_REFUSED) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # `python -m mcp_gateway.cli`, which is what `scripts/start-gateway.sh` -- and therefore
    # `make run`, `make run-dev` and the launchd plist -- invokes.
    #
    # Without this guard the module is imported as `__main__`, defines everything, and
    # exits 0 without binding anything. The banner still prints, because the Makefile prints
    # it, so the failure looks exactly like a daemon that started and is quietly serving.
    # The console script (`mcp-gateway`) calls `run()` through its entry point and was never
    # affected, which is why this survived: every test and every hand-run used that path.
    run()
