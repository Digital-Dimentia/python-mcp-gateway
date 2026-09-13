"""`logging/setLevel` down, `notifications/message` back up, and the filter between them.

The two directions do not carry the same level, and that is the whole design. One backend
process serves every client and MCP has no per-subscriber level on its wire, so what goes
*down* is the most verbose level anybody asked for, and what comes *up* is filtered per
session against the level that session set. A client asking for `debug` therefore makes the
daemon noisier for itself alone.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp_gateway import errors, protocol
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  talker:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "talker"
      MOCK_LOGGING: "1"
  quiet:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "quiet"
"""


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def messages(client) -> list:
    return [n for n in client.notifications if n.get("method") == "notifications/message"]


async def emit(client, level: str, text: str = "hello", logger: str | None = None) -> None:
    args = {"level": level, "text": text}
    if logger:
        args["logger"] = logger
    await client.call("tools/call", {"name": "talker__log", "arguments": args})


# --- the ordering helpers, on their own -------------------------------------------------


def test_a_threshold_admits_its_own_level_and_everything_above() -> None:
    assert protocol.level_admits("warning", "warning") is True
    assert protocol.level_admits("warning", "error") is True
    assert protocol.level_admits("warning", "info") is False
    assert protocol.level_admits("debug", "emergency") is True


def test_an_unknown_level_is_admitted_rather_than_swallowed() -> None:
    """A later revision may add one, and dropping a message because we do not recognise its
    severity is the one failure a log relay must not have."""
    assert protocol.level_admits("warning", "catastrophe") is True


def test_the_union_is_the_most_verbose_level_anyone_asked_for() -> None:
    assert protocol.most_verbose({"warning", "debug", "error"}) == "debug"
    assert protocol.most_verbose({"error"}) == "error"
    assert protocol.most_verbose(set()) is None


# --- down the wire ------------------------------------------------------------------------


async def test_logging_is_advertised(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect(initialize=False)
        assert "logging" in (await client.initialize())["capabilities"]
    finally:
        await harness.close()


async def test_set_level_reaches_the_backend(tmp_path) -> None:
    """The `log` tool reports the level this process was told, so a level that stopped at
    the gateway is distinguishable from one that arrived."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert await client.call("logging/setLevel", {"level": "debug"}) == {}
        assert await client.tool_text("talker__log", {"level": "info"}) == "level=debug"
    finally:
        await harness.close()


async def test_a_backend_without_logging_is_skipped_not_refused(tmp_path) -> None:
    """`quiet` declares no `logging`. Asking it would be a `-32601` the client caused and
    could do nothing about."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert await client.call("logging/setLevel", {"level": "debug"}) == {}
        assert await client.tool_text("quiet__echo", {"text": "still here"}) == "quiet:echo:still here"
    finally:
        await harness.close()


async def test_an_unknown_level_is_refused(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request("logging/setLevel", {"level": "chatty"})
        assert response["error"]["code"] == errors.INVALID_PARAMS
    finally:
        await harness.close()


# --- back up the wire ---------------------------------------------------------------------


async def test_a_backend_message_reaches_a_client_that_asked(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.call("logging/setLevel", {"level": "info"})
        await emit(client, "error", "the disk is full")
        assert await wait_for(lambda: messages(client))
        assert messages(client)[0]["params"]["data"] == "the disk is full"
    finally:
        await harness.close()


async def test_the_message_says_which_backend_spoke(tmp_path) -> None:
    """Three backends' logs in one stream are useless if you cannot tell them apart, and
    `logger` is the field MCP already has for saying so."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.call("logging/setLevel", {"level": "info"})
        await emit(client, "warning")
        assert await wait_for(lambda: messages(client))
        assert messages(client)[0]["params"]["logger"] == "talker"

        # A backend that names its own logger keeps it, behind the backend's name.
        await emit(client, "warning", logger="db.pool")
        assert await wait_for(lambda: len(messages(client)) > 1)
        assert messages(client)[-1]["params"]["logger"] == "talker/db.pool"
    finally:
        await harness.close()


async def test_a_client_that_never_asked_is_sent_nothing(tmp_path) -> None:
    """Silence is the default. The spec leaves it to the server, and every client that
    predates this feature would otherwise start receiving a stream it never requested."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        asker = await harness.connect()
        silent = await harness.connect()
        await asker.call("logging/setLevel", {"level": "debug"})
        await emit(asker, "error")
        assert await wait_for(lambda: messages(asker))
        await asyncio.sleep(0.3)
        assert messages(silent) == []
    finally:
        await harness.close()


async def test_each_client_is_filtered_against_its_own_level(tmp_path) -> None:
    """The reason the two directions differ. `debug` goes down because one client wants it;
    the client that asked for `error` must still not see the `info`."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        verbose = await harness.connect()
        terse = await harness.connect()
        await verbose.call("logging/setLevel", {"level": "debug"})
        await terse.call("logging/setLevel", {"level": "error"})

        await emit(verbose, "info", "chatter")
        assert await wait_for(lambda: messages(verbose))
        await emit(verbose, "critical", "the roof is on fire")
        assert await wait_for(lambda: messages(terse))

        assert [m["params"]["data"] for m in messages(verbose)] == ["chatter", "the roof is on fire"]
        assert [m["params"]["data"] for m in messages(terse)] == ["the roof is on fire"]
    finally:
        await harness.close()


async def test_the_level_is_replayed_to_a_restarted_backend(tmp_path) -> None:
    """The process that comes back has never heard it, and the client is not going to ask
    again -- the same argument as a replayed subscription."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.call("logging/setLevel", {"level": "notice"})
        await harness.gateway.restart_backend("talker")
        assert await client.tool_text("talker__log", {"level": "info"}) == "level=notice"
    finally:
        await harness.close()


async def test_the_union_falls_when_the_verbose_client_leaves(tmp_path) -> None:
    """Nothing else would recompute it, and the daemon would stay at `debug` for a client
    that is no longer connected."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        verbose = await harness.connect()
        terse = await harness.connect()
        await verbose.call("logging/setLevel", {"level": "debug"})
        await terse.call("logging/setLevel", {"level": "error"})
        assert await terse.tool_text("talker__log", {"level": "error"}) == "level=debug"

        await verbose.close()
        assert await wait_for(lambda: len(harness.gateway.describe_connections()) == 1)
        assert await terse.tool_text("talker__log", {"level": "error"}) == "level=error"
    finally:
        await harness.close()
