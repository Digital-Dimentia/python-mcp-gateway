"""A backend that failed to start is tried again, without anybody asking.

The case this exists for is ordinary: compose starts a gateway alongside the containers it
proxies, and whichever one is not listening in the second the gateway dials it was recorded
as failed and stayed that way -- with `curl` on the box reaching the port perfectly well,
and a `SIGHUP` no help, because reload restarts only what *changed* and nothing had.

The machinery was almost all there already. `Backend._fail` has always set the backoff
floor; what was missing was anything that ever looked at it again.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_gateway.backend import BackendStatus
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"


def working(name: str) -> str:
    return f'  {name}:\n    command: {sys.executable}\n    args: ["{FIXTURE}"]\n'


def broken(name: str) -> str:
    """A command that exits at once, which is what a failed start looks like."""
    return f"  {name}:\n    command: /usr/bin/false\n"


def until(name: str, marker: Path) -> str:
    """A backend that fails until `marker` exists, then works.

    A dependency arriving late is the case this feature is for, and simulating it this way
    -- rather than reaching in and editing a frozen `ServerSpec` -- means the test exercises
    the same path a real one would: the same command, run again, working this time.
    """
    script = f'test -f {marker} && exec {sys.executable} {FIXTURE} || exit 1'
    return (
        f"  {name}:\n"
        f"    command: /bin/sh\n"
        f'    args: ["-c", "{script}"]\n'
        f"    env_passthrough: [\"PATH\"]\n"
    )


def disabled(name: str) -> str:
    return f"  {name}:\n    command: /usr/bin/false\n    enabled: false\n"


async def test_a_failed_backend_is_retried_when_its_backoff_has_passed(tmp_path) -> None:
    harness = await daemon(tmp_path, servers="servers:\n" + broken("nope"))
    try:
        backend = harness.gateway.supervisor.get("nope")
        assert backend.status is BackendStatus.FAILED
        before = backend.consecutive_failures

        # The first retry's floor is zero, deliberately -- see `restart_backoff_seconds`.
        backend._retry_at = None
        await harness.gateway.recover_once()

        assert backend.consecutive_failures > before, "it must actually have been tried"
    finally:
        await harness.close()


async def test_a_backend_still_cooling_off_is_left_alone(tmp_path) -> None:
    """The backoff is what keeps a broken backend from being hammered once a sweep."""
    harness = await daemon(tmp_path, servers="servers:\n" + broken("nope"))
    try:
        backend = harness.gateway.supervisor.get("nope")
        backend._retry_at = float("inf")
        before = backend.consecutive_failures

        assert await harness.gateway.recover_once() == []
        assert backend.consecutive_failures == before, "a refusal is not an attempt"
    finally:
        await harness.close()


async def test_a_disabled_backend_is_never_revived(tmp_path) -> None:
    """`DISABLED` is somebody's decision; reviving it would be the loop overruling a person."""
    harness = await daemon(tmp_path, servers="servers:\n" + disabled("off"))
    try:
        backend = harness.gateway.supervisor.get("off")
        assert backend.status is BackendStatus.DISABLED

        assert await harness.gateway.recover_once() == []
        assert backend.status is BackendStatus.DISABLED
    finally:
        await harness.close()


async def test_a_stopped_backend_is_never_revived(tmp_path) -> None:
    """Same rule, different reason: somebody turned this one off."""
    harness = await daemon(tmp_path, servers="servers:\n" + working("a"))
    try:
        backend = harness.gateway.supervisor.get("a")
        await backend.stop()
        assert backend.status is BackendStatus.STOPPED

        assert await harness.gateway.recover_once() == []
        assert backend.status is BackendStatus.STOPPED
    finally:
        await harness.close()


async def test_a_running_backend_is_not_disturbed(tmp_path) -> None:
    harness = await daemon(tmp_path, servers="servers:\n" + working("a"))
    try:
        backend = harness.gateway.supervisor.get("a")
        pid_before = backend.pid

        assert await harness.gateway.recover_once() == []
        assert backend.pid == pid_before, "a healthy backend must not be restarted under it"
    finally:
        await harness.close()


async def test_a_backend_that_comes_back_is_announced_and_its_tools_appear(tmp_path) -> None:
    """A recovered backend publishes tools that were not there a moment ago, and a client
    holding the old listing has no way to find that out on its own.

    Repaired by pointing the *same name* at a command that works, without a reload, so the
    only thing that can bring it back is the sweep.
    """
    marker = tmp_path / "dependency-is-up"
    harness = await daemon(tmp_path, servers="servers:\n" + until("late", marker))
    try:
        client = await harness.connect()
        backend = harness.gateway.supervisor.get("late")
        assert backend.status is BackendStatus.FAILED
        assert not [t for t in (await client.call("tools/list"))["tools"] if t["name"].startswith("late__")]

        # The thing it was waiting for arrives.
        marker.write_text("up", encoding="utf-8")
        backend._retry_at = None
        client.notifications.clear()

        assert await harness.gateway.recover_once() == ["late"]
        assert backend.status is BackendStatus.RUNNING

        # Then a round trip, for the reason `Client.initialize` gives: a notification has
        # no reply of its own, so without it the test reads the list before the socket has
        # delivered anything.
        await harness.gateway.notifier.flush()
        await client.call("ping")
        methods = {n["method"] for n in client.notifications}
        assert "notifications/tools/list_changed" in methods, client.notifications

        listed = [t for t in (await client.call("tools/list"))["tools"] if t["name"].startswith("late__")]
        assert listed, "the recovered backend's tools must actually be published"
    finally:
        await harness.close()


async def test_a_failed_attempt_announces_nothing(tmp_path) -> None:
    """Announcing once a minute for a backend that is simply broken would be a storm about
    no news, so the unattended path is the one caller that reports only success."""
    harness = await daemon(tmp_path, servers="servers:\n" + broken("nope"))
    try:
        client = await harness.connect()
        backend = harness.gateway.supervisor.get("nope")
        backend._retry_at = None
        client.notifications.clear()

        assert await harness.gateway.recover_once() == []
        await harness.gateway.notifier.flush()
        await client.call("ping")
        assert client.notifications == [], "a retry that failed is not news"
    finally:
        await harness.close()
