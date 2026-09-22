"""What happens when a backend is not there, per method.

The distinction these assert is the most user-visible decision in the project: a tool call
on a down backend comes back as *content a model can read*, while a prompt or a resource --
which have no `isError` -- come back as protocol errors.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from mcp_gateway import errors
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

CRASHER = f"""
servers:
  crashy:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "crashy"
      MOCK_TOOLS: "fine,boom"
      MOCK_CRASH_ON_CALL: "boom"
      MOCK_RESOURCES: "file:///a.txt"
      MOCK_PROMPTS: "greet"
"""

UNCONFIGURED = f"""
servers:
  github:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      TOKEN: "${{MISSING_TOKEN}}"
"""


async def test_a_backend_with_a_missing_secret_is_skipped_and_the_daemon_serves(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        client = await harness.connect()
        assert "gateway__list_backends" in await client.tool_names()
        assert harness.gateway.backend("github").status.value == "failed"
    finally:
        await harness.close()


#: A server that refuses to start and says why, which is what every real one does: the
#: filesystem server told a directory that is not there prints exactly this shape.
REFUSES = """
servers:
  fs:
    command: /bin/sh
    args: ["-c", "echo 'Error: Directory /path/to/your/code does not exist' >&2; exit 1"]
"""


async def test_a_backend_that_refuses_to_start_is_failed_in_its_own_words(tmp_path) -> None:
    """`MCP process closed stdout` is true of a missing directory and of a bad token alike.

    The thing that tells them apart is on stderr, which went to the debug log -- and nobody
    reading `backend fs not started` has a reason to reach for `--debug`. So the server's
    last words come with the failure, and the exit status with them.
    """
    harness = await daemon(tmp_path, servers=REFUSES)
    try:
        error = harness.gateway.backend("fs").error
        assert "/path/to/your/code does not exist" in error
        assert "exited with status 1" in error
    finally:
        await harness.close()


async def test_a_silent_refusal_still_says_what_it_can(tmp_path) -> None:
    """No stderr to quote is not a reason to say less than was known."""
    harness = await daemon(tmp_path, servers="servers:\n  quiet:\n    command: /usr/bin/false\n")
    try:
        error = harness.gateway.backend("quiet").error
        assert "closed stdout" in error and "exited with status 1" in error
        assert "last stderr" not in error
    finally:
        await harness.close()


def test_a_clip_keeps_both_ends_and_counts_what_it_took() -> None:
    """The unit behind it: exact width, both ends intact, the elision stated."""
    from mcp_gateway.mcp_stdio import clip_middle

    assert clip_middle("short", width=10, head=6) == "short"
    assert clip_middle("0123456789", width=10, head=6) == "0123456789"

    clipped = clip_middle("A" * 400 + "B" * 300 + "C" * 200, width=600, head=380)
    assert clipped.startswith("A" * 380) and clipped.endswith("B" * 20 + "C" * 200)
    assert "[...300 more chars...]" in clipped
    assert len(clipped) == 600 + len("[...300 more chars...]")


async def test_a_long_refusal_keeps_both_ends_and_admits_the_elision(tmp_path) -> None:
    """A refusal puts the name at the front and the reason at the back. Take from the middle.

    `python3: can\'t open file \'<long path>\': [Errno 2] No such file or directory` clipped
    from the right loses the reason and the last stretch of the path, and what a person then
    reads is a path that looks truncated -- so the hunt starts on the catalogue, which is
    fine, instead of on the file, which is missing.
    """
    long_path = "/srv/" + "deep/" * 120 + "server/main.py"
    line = f"python3: can't open file '{long_path}': [Errno 2] No such file or directory"
    args = json.dumps(["-c", f"echo {shlex.quote(line)} >&2; exit 1"])
    servers = f"servers:\n  long:\n    command: /bin/sh\n    args: {args}\n"
    harness = await daemon(tmp_path, servers=servers)
    try:
        error = harness.gateway.backend("long").error
        assert "python3: can't open file" in error
        assert "[Errno 2] No such file or directory" in error, "the reason was clipped away"
        assert "more chars..." in error, "an elision has to say it happened"
    finally:
        await harness.close()


async def test_a_command_line_that_will_not_spawn_shows_its_whitespace(tmp_path) -> None:
    """`No such file or directory: \'python3\'` blames the interpreter for a broken argument.

    A path carrying a stray space -- from a hand-folded catalogue line, or a paste that
    wrapped -- is the case this exists for. `shlex.join` quotes only the arguments holding
    whitespace, so the error points at the one that is wrong instead of at the command.
    """
    servers = (
        "servers:\n"
        "  spaced:\n"
        "    command: /usr/bin/does-not-exist\n"
        "    args: [\"/srv/not-a-full-wor d/main.py\"]\n"
    )
    harness = await daemon(tmp_path, servers=servers)
    try:
        error = harness.gateway.backend("spaced").error
        assert "/usr/bin/does-not-exist" in error
        assert "'/srv/not-a-full-wor d/main.py'" in error, error
    finally:
        await harness.close()


async def test_calling_a_down_backends_tool_returns_readable_content(tmp_path) -> None:
    """The decision that matters.

    A model that reads "missing secret MISSING_TOKEN" tells the human GitHub is not
    configured. A model that gets -32603 gets a turn that died on a protocol error.
    """
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        client = await harness.connect()
        response = await client.request(
            "tools/call", {"name": "github__anything", "arguments": {}}
        )
        assert "error" not in response, "must not be a protocol error"
        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "github" in text
        assert "MISSING_TOKEN" in text
        assert "gateway__backend_health" in text
    finally:
        await harness.close()


async def test_a_down_backends_prompt_is_a_protocol_error(tmp_path) -> None:
    """No `isError` on a prompt result, so there is no escape hatch to use."""
    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        client = await harness.connect()
        response = await client.request("prompts/get", {"name": "github__greet"})
        assert response["error"]["code"] == errors.INTERNAL_ERROR
        assert response["error"]["data"]["backend"] == "github"
    finally:
        await harness.close()


async def test_a_down_backends_resource_is_resource_not_found(tmp_path) -> None:
    """MCP's dedicated code: the URI names something not currently readable."""
    from mcp_gateway.naming import encode_resource_uri

    harness = await daemon(tmp_path, servers=UNCONFIGURED)
    try:
        client = await harness.connect()
        uri = encode_resource_uri("github", "file:///x")
        response = await client.request("resources/read", {"uri": uri})
        assert response["error"]["code"] == errors.RESOURCE_NOT_FOUND
    finally:
        await harness.close()


async def test_a_backend_that_crashes_mid_call_does_not_take_the_turn_down(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=CRASHER)
    try:
        client = await harness.connect()
        assert await client.tool_text("crashy__fine", {"text": "ok"}) == "crashy:fine:ok"

        response = await client.request("tools/call", {"name": "crashy__boom", "arguments": {}})
        # The transport died mid-request, so this is *ours*: no `source`, because the
        # backend never produced a code.
        assert response["error"]["code"] == errors.INTERNAL_ERROR
        assert "source" not in response["error"]["data"]
        assert response["error"]["data"]["backend"] == "crashy"
    finally:
        await harness.close()


async def test_health_reports_a_crashed_backend_and_restart_brings_it_back(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=CRASHER)
    try:
        client = await harness.connect()
        await client.request("tools/call", {"name": "crashy__boom", "arguments": {}})

        health = json.loads(
            (
                await client.call(
                    "tools/call",
                    {"name": "gateway__backend_health", "arguments": {"name": "crashy"}},
                )
            )["content"][0]["text"]
        )
        assert health["ping_ms"] is None, "a dead backend cannot answer a ping"

        restart = json.loads(
            (
                await client.call(
                    "tools/call",
                    {"name": "gateway__restart_backend", "arguments": {"name": "crashy"}},
                )
            )["content"][0]["text"]
        )
        assert restart["running"] is True
        assert await client.tool_text("crashy__fine", {"text": "back"}) == "crashy:fine:back"
    finally:
        await harness.close()


async def test_a_restart_invalidates_the_cached_listing(tmp_path) -> None:
    """Whatever it published before is not necessarily what it publishes now."""
    harness = await daemon(tmp_path, servers=CRASHER)
    try:
        client = await harness.connect()
        await client.call("tools/list")
        assert harness.gateway.catalogue.counts("crashy")["tool_count"] is not None
        await harness.gateway.restart_backend("crashy")
        assert harness.gateway.catalogue.counts("crashy")["tool_count"] is None
    finally:
        await harness.close()


async def test_restarting_an_unknown_backend_is_a_tool_level_error(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=CRASHER)
    try:
        client = await harness.connect()
        result = await client.call(
            "tools/call", {"name": "gateway__restart_backend", "arguments": {"name": "nope"}}
        )
        assert result["isError"] is True
    finally:
        await harness.close()
