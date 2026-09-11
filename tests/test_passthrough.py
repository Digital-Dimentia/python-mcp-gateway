"""Reverse passthrough: a backend asking the client a question, through the gateway.

The interesting part is not the forwarding. It is deciding *which* client to ask when
several are attached, and refusing when the honest answer is that there is nobody to ask.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

ROOTS = {"roots": {"listChanged": False}}
ELICIT = {"elicitation": {}}

ASKS_ROOTS = f"""
servers:
  a:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "a"
      MOCK_TOOLS: "ask"
      MOCK_ASK_ROOTS: "1"
"""

ASKS_ELICIT = f"""
servers:
  a:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "a"
      MOCK_TOOLS: "ask"
      MOCK_ASK_ELICIT: "1"
"""


async def test_a_backends_roots_request_reaches_the_calling_client(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        client = await harness.connect(capabilities=ROOTS)
        # The backend was started before the client attached, so it carries no capability
        # yet; a restart is the one moment it can be told otherwise.
        await harness.gateway.restart_backend("a")
        client.answer_with = {"roots": [{"uri": "file:///work", "name": "work"}]}

        answered = json.loads(await client.tool_text("a__ask", {}))
        assert answered["roots"]["roots"][0]["uri"] == "file:///work"
        assert [r["method"] for r in client.server_requests] == ["roots/list"]
    finally:
        await harness.close()


async def test_an_elicitation_reaches_the_calling_client(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=ASKS_ELICIT)
    try:
        client = await harness.connect(capabilities=ELICIT)
        await harness.gateway.restart_backend("a")
        client.answer_with = {"action": "accept", "content": {"count": 3}}

        answered = json.loads(await client.tool_text("a__ask", {}))
        assert answered["elicit"]["content"]["count"] == 3
    finally:
        await harness.close()


async def test_it_reaches_the_calling_client_and_not_the_other_one(tmp_path) -> None:
    """The origin-connection rule. Asking the wrong human is worse than not asking."""
    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        caller = await harness.connect(capabilities=ROOTS)
        bystander = await harness.connect(capabilities=ROOTS)
        await harness.gateway.restart_backend("a")
        caller.answer_with = {"roots": []}
        bystander.answer_with = {"roots": [{"uri": "file:///wrong", "name": "wrong"}]}

        await caller.tool_text("a__ask", {})
        assert [r["method"] for r in caller.server_requests] == ["roots/list"]
        assert bystander.server_requests == [], "the bystander must not be interrupted"
    finally:
        await harness.close()


async def test_a_client_that_declared_nothing_is_not_asked(tmp_path) -> None:
    """A capability block is a promise in both directions.

    We never declared `roots` to the backend, so it should not ask -- and if it asks anyway,
    we must not put the question to a client that never offered to answer it.
    """
    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        client = await harness.connect()  # declares nothing
        await harness.gateway.restart_backend("a")

        answered = json.loads(await client.tool_text("a__ask", {}))
        # The backend gets -32601, which is exactly what an undeclared capability means.
        assert answered["roots"]["code"] == -32601
        assert client.server_requests == []
    finally:
        await harness.close()


async def test_a_spontaneous_backend_request_is_refused(tmp_path) -> None:
    """No call in flight means no honest choice of whose human to interrupt."""
    from mcp_gateway.mcp_stdio import UnsupportedServerRequest

    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        await harness.connect(capabilities=ROOTS)
        try:
            await harness.gateway.backend_request("a", "roots/list", {})
            raise AssertionError("a spontaneous request should be refused")
        except UnsupportedServerRequest as refusal:
            assert "no single client request in flight" in str(refusal)
    finally:
        await harness.close()


async def test_two_clients_mid_call_on_one_backend_make_the_origin_ambiguous(tmp_path) -> None:
    """MCP gives no correlation between a server-to-client request and the call that
    provoked it. With two clients mid-call, guessing has a fifty percent chance of putting
    one client's question in front of another client's user, so we refuse instead."""
    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        one = await harness.connect(capabilities=ROOTS)
        two = await harness.connect(capabilities=ROOTS)
        backend = harness.gateway.supervisor.get("a")
        sessions = list(harness.gateway._sessions)
        assert len(sessions) == 2

        with backend.serving(sessions[0]):
            assert backend.origin_session() is sessions[0]
            with backend.serving(sessions[1]):
                assert backend.origin_session() is None, "ambiguous, so no answer"
            assert backend.origin_session() is sessions[0]
        assert backend.origin_session() is None
        assert one is not two
    finally:
        await harness.close()


async def test_one_client_with_two_calls_in_flight_is_still_unambiguous(tmp_path) -> None:
    """Concurrency from a single client is the ordinary case, not an ambiguity."""
    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        await harness.connect(capabilities=ROOTS)
        backend = harness.gateway.supervisor.get("a")
        session = next(iter(harness.gateway._sessions))
        with backend.serving(session), backend.serving(session):
            assert backend.origin_session() is session
        assert backend.origin_session() is None
    finally:
        await harness.close()


async def test_a_method_the_gateway_does_not_relay_is_refused(tmp_path) -> None:
    from mcp_gateway.mcp_stdio import UnsupportedServerRequest

    harness = await daemon(tmp_path, servers=ASKS_ROOTS)
    try:
        await harness.connect(capabilities=ROOTS)
        try:
            await harness.gateway.backend_request("a", "something/invented", {})
            raise AssertionError("an unrelayed method should be refused")
        except UnsupportedServerRequest:
            pass
    finally:
        await harness.close()


async def test_a_clients_error_answer_is_handed_to_the_backend_verbatim(tmp_path) -> None:
    """A client refusing an elicitation is information the backend asked for, not a
    gateway failure to be papered over."""
    import asyncio

    from mcp_gateway import errors, jsonrpc
    from mcp_gateway.mcp_stdio import MCPProtocolError

    harness = await daemon(tmp_path, servers=ASKS_ELICIT)
    try:
        client = await harness.connect(capabilities=ELICIT)
        session = next(iter(harness.gateway._sessions))
        backend = harness.gateway.supervisor.get("a")

        # Answer the next gateway->client request with an error.
        async def refuse(message):
            await client._send(
                jsonrpc.failure(
                    message["id"], errors.error_object(-32099, "the human said no", {"why": "busy"})
                )
            )

        client.answer_with = None
        original = client.server_requests

        with backend.serving(session):
            task = asyncio.create_task(
                harness.gateway.backend_request("a", "elicitation/create", {"message": "?"})
            )
            for _ in range(200):
                if len(client.server_requests) > len(original):
                    break
                await asyncio.sleep(0.01)
            await refuse(client.server_requests[-1])
            try:
                await task
                raise AssertionError("the client's refusal should reach the backend")
            except MCPProtocolError as exc:
                assert exc.code == -32099
                assert "the human said no" in str(exc)
                assert exc.data == {"why": "busy"}
    finally:
        await harness.close()
