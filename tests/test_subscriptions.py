"""`resources/subscribe`, and the `resources/updated` that comes back through it.

Two things have to be true for a subscription to be worth advertising, and they are
separable: the notification has to reach the client that asked, and it has to reach *only*
that client, carrying a URI in the gateway's address space rather than the backend's. The
second is what makes it usable — a client that only ever saw `mcpgw://docs/...` cannot match
a `file:///README.md` against anything it holds.

The unit half of this file is `Subscriptions` on its own, because the two indexes it keeps
have to stay in step through every removal path and none of those are visible end to end.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp_gateway import errors
from mcp_gateway.naming import encode_resource_uri
from mcp_gateway.notifications import Subscriptions
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

README = "file:///README.md"

#: `docs` and `code` both publish `file:///README.md` — the collision that made URI
#: rewriting necessary, and the case that makes "only the right subscriber" mean something.
SERVERS = f"""
servers:
  docs:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "docs"
      MOCK_RESOURCES: "{README},file:///guide.md"
      MOCK_UPDATE_ON_SUBSCRIBE: "1"
  code:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "code"
      MOCK_RESOURCES: "{README}"
      MOCK_UPDATE_ON_SUBSCRIBE: "1"
  plain:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "plain"
      MOCK_RESOURCES: "file:///notes.md"
"""


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def updates(client) -> list:
    return [n for n in client.notifications if n.get("method") == "notifications/resources/updated"]


# --- the registry, on its own -------------------------------------------------------------


def test_the_two_indexes_stay_in_step_through_every_removal() -> None:
    """The reason `Subscriptions` is a class and not two dicts in the gateway. A URI left in
    `_by_uri` after its last subscriber went away is a slow leak; a session left in
    `_by_session` is a notification sent to a closed socket."""
    subs = Subscriptions()
    a, b = object(), object()
    subs.add(a, "mcpgw://docs/x")
    subs.add(b, "mcpgw://docs/x")
    subs.add(a, "mcpgw://code/y")
    assert len(subs) == 3

    assert subs.remove(a, "mcpgw://docs/x") is True
    assert subs.sessions_for("mcpgw://docs/x") == [b]
    assert subs.uris_of(a) == frozenset({"mcpgw://code/y"})

    subs.drop_session(b)
    assert subs.sessions_for("mcpgw://docs/x") == []
    subs.drop_backend("code")
    assert len(subs) == 0
    assert subs.uris_of(a) == frozenset()


def test_a_duplicate_subscription_is_reported_so_the_wire_call_can_be_skipped() -> None:
    subs = Subscriptions()
    session = object()
    assert subs.add(session, "mcpgw://docs/x") is True
    assert subs.add(session, "mcpgw://docs/x") is False
    assert len(subs) == 1
    assert subs.remove(session, "mcpgw://docs/x") is True
    assert subs.remove(session, "mcpgw://docs/x") is False


def test_uris_for_matches_the_backend_and_not_a_name_that_starts_with_it() -> None:
    """`docs` must not collect `docsearch`'s subscriptions when a reload removes it."""
    subs = Subscriptions()
    session = object()
    subs.add(session, encode_resource_uri("docs", README))
    subs.add(session, encode_resource_uri("docsearch", README))
    assert subs.uris_for("docs") == [encode_resource_uri("docs", README)]


# --- end to end -----------------------------------------------------------------------------


async def test_subscribe_is_advertised_and_answered(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        assert (await client.initialize())["capabilities"]["resources"]["subscribe"] is True
        assert await client.call(
            "resources/subscribe", {"uri": encode_resource_uri("docs", README)}
        ) == {}
    finally:
        await harness.close()


async def test_an_update_arrives_with_the_uri_rewritten(tmp_path) -> None:
    """The backend says `file:///README.md`; the client must be told `mcpgw://docs/...`.

    The backend never sees the public form, so a notification carrying it can only have
    been rewritten on the way through.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        public = encode_resource_uri("docs", README)
        await client.call("resources/subscribe", {"uri": public})
        assert await wait_for(lambda: updates(client))
        assert updates(client)[0]["params"]["uri"] == public
    finally:
        await harness.close()


async def test_only_the_subscriber_hears_about_it(tmp_path) -> None:
    """Two clients, one subscription. The other is entitled to silence — that is what
    subscribing *means*, and it is the difference between this and a broadcast."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        subscriber = await harness.connect()
        bystander = await harness.connect()
        await subscriber.call(
            "resources/subscribe", {"uri": encode_resource_uri("docs", README)}
        )
        assert await wait_for(lambda: updates(subscriber))
        # Long enough that a broadcast would have arrived by now.
        await asyncio.sleep(0.3)
        assert updates(bystander) == []
    finally:
        await harness.close()


async def test_the_other_backends_identical_uri_is_a_different_subscription(tmp_path) -> None:
    """`docs` and `code` both publish `file:///README.md`. A change in one must not wake a
    client watching the other — the collision `encode_resource_uri` exists to prevent,
    reaching the notification path this time."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        await client.call("resources/subscribe", {"uri": encode_resource_uri("code", README)})
        assert await wait_for(lambda: updates(client))
        assert all(
            n["params"]["uri"] == encode_resource_uri("code", README) for n in updates(client)
        )
    finally:
        await harness.close()


async def test_unsubscribing_stops_the_notifications(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        public = encode_resource_uri("docs", README)
        await client.call("resources/subscribe", {"uri": public})
        assert await wait_for(lambda: updates(client))
        assert await client.call("resources/unsubscribe", {"uri": public}) == {}
        assert harness.gateway.subscriptions.sessions_for(public) == []
    finally:
        await harness.close()


async def test_a_closed_connection_takes_its_subscriptions_with_it(tmp_path) -> None:
    """Otherwise the registry grows for the life of the daemon, and every update is relayed
    to a socket nobody is reading."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        public = encode_resource_uri("docs", README)
        await client.call("resources/subscribe", {"uri": public})
        assert len(harness.gateway.subscriptions) == 1
        await client.close()
        assert await wait_for(lambda: len(harness.gateway.subscriptions) == 0)
    finally:
        await harness.close()


async def test_a_backend_without_subscribe_refuses_its_own_uri(tmp_path) -> None:
    """And with `-32002`, not `-32601`. A method-not-found names a method the client did
    call and the gateway does implement, which reads as a gateway bug; this URI simply
    cannot be watched."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request(
            "resources/subscribe", {"uri": encode_resource_uri("plain", "file:///notes.md")}
        )
        assert response["error"]["code"] == errors.RESOURCE_NOT_FOUND
        assert "subscription" in response["error"]["message"]
    finally:
        await harness.close()


async def test_an_unknown_uri_is_refused_and_records_nothing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        response = await client.request(
            "resources/subscribe", {"uri": encode_resource_uri("nope", README)}
        )
        assert response["error"]["code"] == errors.RESOURCE_NOT_FOUND
        assert len(harness.gateway.subscriptions) == 0
    finally:
        await harness.close()


async def test_a_subscription_is_replayed_after_a_restart(tmp_path) -> None:
    """The process that comes back has never heard of it. Without the replay the client's
    subscription silently stops producing, which looks exactly like a resource that stopped
    changing — the failure this is here to prevent."""
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        client = await harness.connect()
        public = encode_resource_uri("docs", README)
        await client.call("resources/subscribe", {"uri": public})
        assert await wait_for(lambda: updates(client))
        before = len(updates(client))

        await harness.gateway.restart_backend("docs")
        # The replayed subscribe provokes a fresh update from the new process.
        assert await wait_for(lambda: len(updates(client)) > before)
        assert updates(client)[-1]["params"]["uri"] == public
        assert harness.gateway.subscriptions.sessions_for(public) != []
    finally:
        await harness.close()
