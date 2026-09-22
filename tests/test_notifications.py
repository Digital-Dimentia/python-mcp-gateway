"""What reaches a client when a backend speaks up, and what deliberately does not."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp_gateway.notifications import Notifier
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable


def entry(name: str, **env: str) -> str:
    lines = [f"  {name}:", f"    command: {PY}", f'    args: ["{FIXTURE}"]']
    if env:
        lines.append("    env:")
        lines += [f'      {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines) + "\n"


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def of_kind(client, method: str) -> list:
    return [n for n in client.notifications if n.get("method") == method]


# --- the debounce, in isolation ----------------------------------------------------------


async def test_a_burst_of_list_changed_becomes_one_notification() -> None:
    """A reload restarting eight backends must not cost the client eight fan-out refetches."""
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=0.05)
    for _ in range(8):
        await notifier.list_changed("notifications/tools/list_changed")
    await asyncio.sleep(0.15)
    assert emitted == ["notifications/tools/list_changed"]


async def test_a_steady_trickle_still_fires() -> None:
    """The classic debounce bug: restarting the timer means the busiest case never fires."""
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=0.05)
    for _ in range(6):
        await notifier.list_changed("notifications/tools/list_changed")
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.1)
    assert emitted, "a continuous trickle must not postpone the notification forever"


async def test_kinds_are_debounced_independently() -> None:
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=0.05)
    await notifier.list_changed("notifications/tools/list_changed")
    await notifier.list_changed("notifications/prompts/list_changed")
    await asyncio.sleep(0.15)
    assert set(emitted) == {
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
    }


# --- against a real backend ---------------------------------------------------------------


async def test_a_backends_list_changed_reaches_the_client(tmp_path) -> None:
    config = "servers:\n" + entry("a", MOCK_LIST_CHANGED_AFTER_MS="150")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        assert await wait_for(lambda: of_kind(client, "notifications/tools/list_changed"))
    finally:
        await harness.close()


async def test_it_reaches_every_attached_client(tmp_path) -> None:
    config = "servers:\n" + entry("a", MOCK_LIST_CHANGED_AFTER_MS="200")
    harness = await daemon(tmp_path, servers=config)
    try:
        one = await harness.connect()
        two = await harness.connect()
        assert await wait_for(
            lambda: of_kind(one, "notifications/tools/list_changed")
            and of_kind(two, "notifications/tools/list_changed")
        )
    finally:
        await harness.close()


async def test_it_invalidates_the_cache_it_is_about(tmp_path) -> None:
    """Which is the whole point of the notification, and is done before it is relayed."""
    config = "servers:\n" + entry("a", MOCK_LIST_CHANGED_AFTER_MS="150")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        await client.call("tools/list")
        assert harness.gateway.catalogue.counts("a")["tool_count"] is not None
        assert await wait_for(
            lambda: harness.gateway.catalogue.counts("a")["tool_count"] is None
        )
    finally:
        await harness.close()


async def test_a_restart_announces_the_catalogue_changed(tmp_path) -> None:
    """The reason the UI went on showing a backend's old prompts after a restart.

    A restart invalidates that backend's cache, but a client holding the old listing has no
    way to find that out; `reload` has always said so and this path did not. All three
    kinds, because a restarted backend may have changed any of them.
    """
    harness = await daemon(tmp_path, servers="servers:\n" + entry("a"))
    try:
        client = await harness.connect()
        await client.call("tools/list")
        assert not of_kind(client, "notifications/tools/list_changed")

        await harness.gateway.restart_backend("a")
        for kind in ("tools", "prompts", "resources"):
            assert await wait_for(
                lambda kind=kind: of_kind(client, f"notifications/{kind}/list_changed")
            ), f"no {kind}/list_changed after a restart"
    finally:
        await harness.close()


async def test_hiding_a_tool_announces_only_the_tool_listing(tmp_path) -> None:
    """A model client has to find out its tool list shrank, or it goes on offering tools the
    operator just retired. Only `tools/list_changed`: prompts and resources did not move, and
    this is the first entry here whose cause is an operator rather than a backend speaking up.

    An identical set re-sent is silent -- the broadcast is guarded on whether it changed.
    """
    harness = await daemon(tmp_path, servers="servers:\n" + entry("a", MOCK_TOOLS="echo"))
    try:
        client = await harness.connect()
        admin = await harness.connect("/admin", initialize=False)
        await client.call("tools/list")

        await admin.call("admin.tools.hide", {"name": "a", "tools": ["echo"]})
        assert await wait_for(lambda: of_kind(client, "notifications/tools/list_changed"))
        # Past the debounce, so anything else that was going to arrive has.
        await asyncio.sleep(0.4)
        assert of_kind(client, "notifications/prompts/list_changed") == []
        assert of_kind(client, "notifications/resources/list_changed") == []

        before = len(of_kind(client, "notifications/tools/list_changed"))
        await admin.call("admin.tools.hide", {"name": "a", "tools": ["echo"]})
        await asyncio.sleep(0.4)
        assert len(of_kind(client, "notifications/tools/list_changed")) == before
    finally:
        await harness.close()


async def test_a_backends_log_message_is_not_re_emitted(tmp_path) -> None:
    """`logging` is not advertised, and MCP forbids using a capability the peer did not
    declare. The content still reaches the daemon's own log."""
    config = "servers:\n" + entry("a")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        await harness.gateway.backend_notification(
            "a", "notifications/message", {"level": "warning", "data": "backend says hi"}
        )
        await asyncio.sleep(0.1)
        assert of_kind(client, "notifications/message") == []
    finally:
        await harness.close()


async def test_resources_updated_is_dropped(tmp_path) -> None:
    """`subscribe: false` is advertised, so nobody is expecting these."""
    config = "servers:\n" + entry("a")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        await harness.gateway.backend_notification(
            "a", "notifications/resources/updated", {"uri": "file:///x"}
        )
        await asyncio.sleep(0.1)
        assert of_kind(client, "notifications/resources/updated") == []
    finally:
        await harness.close()


async def test_progress_goes_only_to_the_connection_that_asked(tmp_path) -> None:
    """The progressToken is the calling client's own; another client never issued it.

    Broadcasting would hand every other client a token it never issued -- noise at best, and
    a collision at worst, since two clients can independently pick the same one.
    """
    config = "servers:\n" + entry("a")
    harness = await daemon(tmp_path, servers=config)
    try:
        one = await harness.connect()
        two = await harness.connect()
        backend = harness.gateway.supervisor.get("a")
        session = next(iter(harness.gateway._sessions))

        with backend.serving(session):
            await harness.gateway.backend_notification(
                "a", "notifications/progress", {"progressToken": "t1", "progress": 1}
            )
        await asyncio.sleep(0.1)

        got = [c for c in (one, two) if of_kind(c, "notifications/progress")]
        assert len(got) == 1, "exactly one connection should have heard it"
    finally:
        await harness.close()


async def test_progress_with_no_call_in_flight_goes_nowhere(tmp_path) -> None:
    config = "servers:\n" + entry("a")
    harness = await daemon(tmp_path, servers=config)
    try:
        client = await harness.connect()
        await harness.gateway.backend_notification(
            "a", "notifications/progress", {"progressToken": "t", "progress": 1}
        )
        await asyncio.sleep(0.1)
        assert of_kind(client, "notifications/progress") == []
    finally:
        await harness.close()


# --- flush delivers what is queued, which is the whole reason it is called ----------


async def test_flush_emits_a_pending_notification_rather_than_dropping_it() -> None:
    """`Gateway.stop` calls this before closing the server precisely so that what is queued
    goes out. It used to cancel the task instead, and `_emit_after_delay` re-raises
    `CancelledError` -- so the one call whose name promised delivery was the one that threw
    the notification away, and a client that reconnected through a restart was owed a
    `list_changed` it never got."""
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    # A long debounce, so nothing can fire on its own and pass this by accident.
    notifier = Notifier(broadcast, debounce=30.0)
    await notifier.list_changed("notifications/tools/list_changed")
    assert emitted == [], "precondition: still waiting"

    await notifier.flush()

    assert emitted == ["notifications/tools/list_changed"]


async def test_flush_emits_each_pending_kind_once(tmp_path) -> None:
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=30.0)
    for method in (
        "notifications/tools/list_changed",
        "notifications/prompts/list_changed",
        "notifications/resources/list_changed",
    ):
        await notifier.list_changed(method)
    await notifier.flush()

    assert sorted(emitted) == sorted(
        [
            "notifications/prompts/list_changed",
            "notifications/resources/list_changed",
            "notifications/tools/list_changed",
        ]
    )


async def test_flush_does_not_send_a_second_copy_of_one_that_already_fired() -> None:
    """Hurrying an emission and duplicating it are different things."""
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=0.02)
    await notifier.list_changed("notifications/tools/list_changed")
    await asyncio.sleep(0.1)
    assert emitted == ["notifications/tools/list_changed"], "precondition: it fired on its own"

    await notifier.flush()
    assert emitted == ["notifications/tools/list_changed"]


async def test_flush_with_nothing_pending_is_a_no_op() -> None:
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=30.0)
    await notifier.flush()
    await notifier.flush()
    assert emitted == []


async def test_a_later_notification_still_waits_its_debounce_after_a_flush() -> None:
    """The door `flush` opens must close behind it: a shared flag left set would make every
    later emission immediate, quietly turning the debounce off for the rest of the run."""
    emitted: list[str] = []

    async def broadcast(method, _params):
        emitted.append(method)
        return 1

    notifier = Notifier(broadcast, debounce=0.08)
    await notifier.list_changed("notifications/tools/list_changed")
    await notifier.flush()
    assert len(emitted) == 1

    await notifier.list_changed("notifications/tools/list_changed")
    await asyncio.sleep(0.02)
    assert len(emitted) == 1, "the second one must still be waiting, not fired instantly"
    await asyncio.sleep(0.12)
    assert len(emitted) == 2
