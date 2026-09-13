"""Signal handlers, and the platform whose event loop cannot install them.

`_install_signal_handlers` runs *before* the socket is bound, so anything it raises is not a
missing convenience -- it is a daemon that does not start. On Windows it raised
`NotImplementedError` from `loop.add_signal_handler`, and the desktop shell reported that as
`no port file` with a traceback three frames deep in asyncio.

Windows is not in this project's Python matrix and will not be: the daemon's own CI is
Linux, and the Windows evidence comes from the desktop workflow, which builds an app. So the
Windows *loop* is simulated here rather than run, exactly as the Windows strip list is
asserted on every host in `test_bundle_python.py`.
"""

from __future__ import annotations

import asyncio
import signal
import types

from mcp_gateway import cli


class _WindowsLoop:
    """`add_signal_handler` as asyncio's Windows loops implement it: not at all."""

    def __init__(self) -> None:
        self.woken: list[object] = []

    def add_signal_handler(self, *_args) -> None:
        raise NotImplementedError

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.woken.append(callback)
        callback(*args)


def test_a_loop_without_add_signal_handler_still_gets_handlers(monkeypatch) -> None:
    loop = _WindowsLoop()
    installed: dict[int, object] = {}
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: installed.setdefault(sig, fn))

    ran: list[int] = []
    cli._handle(loop, signal.SIGINT, lambda: ran.append(1))

    assert signal.SIGINT in installed
    # `signal.signal` runs its handler in the interpreter's main thread rather than in the
    # loop, so what it must do is wake the loop -- not touch loop state from under it.
    installed[signal.SIGINT](signal.SIGINT, None)
    assert ran == [1]
    assert loop.woken


def test_a_worker_thread_gets_no_handlers_rather_than_no_daemon(monkeypatch) -> None:
    """`signal.signal` is main-thread only, and the daemon is embeddable."""

    def _refuse(*_args):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(cli.signal, "signal", _refuse)
    cli._handle(_WindowsLoop(), signal.SIGINT, lambda: None)


async def test_installing_handlers_never_raises_on_a_windows_loop(monkeypatch) -> None:
    """The failure itself: this call sits between `Gateway(...)` and the bind."""

    def _unsupported(*_args):
        raise NotImplementedError

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", _unsupported)
    monkeypatch.setattr(cli.signal, "signal", lambda sig, fn: None)

    gateway = types.SimpleNamespace(shutdown_requested=None)
    cli._install_signal_handlers(gateway)

    assert isinstance(gateway.shutdown_requested, asyncio.Event)
    assert not gateway.shutdown_requested.is_set()


async def test_the_shutdown_event_is_what_the_handlers_set() -> None:
    """On a loop that does support them, the wiring is unchanged."""
    gateway = types.SimpleNamespace(shutdown_requested=None)
    cli._install_signal_handlers(gateway)
    try:
        assert isinstance(gateway.shutdown_requested, asyncio.Event)
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(gateway.shutdown_requested.wait(), timeout=2)
    finally:
        loop = asyncio.get_running_loop()
        for name in ("SIGHUP", "SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                loop.remove_signal_handler(sig)
