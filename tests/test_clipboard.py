"""The clipboard: one document, rendered once, served on both sockets.

Two things are worth pinning here and nothing else really is. The first is that
`admin.clipboard.get` on `/admin` and `gateway__clipboard` on `/mcp` return **the same
text** — that is the entire reason the renderer lives in Python, and a drift between them
would show up as the agent being told something other than what the person copied.

The second is that the document never loses anything quietly: a payload too large to carry
says how large it was, and a session longer than the document says how many calls it left
out. A briefing that silently drops half a bench session is worse than no briefing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_gateway import clipboard
from tests.fixtures.ws_client import daemon

FIXTURE = Path(__file__).parent / "fixtures" / "mock_backend.py"
PY = sys.executable

SERVERS = f"""
servers:
  alpha:
    command: {PY}
    args: ["{FIXTURE}"]
    env:
      MOCK_NAME: "alpha"
"""

SNAPSHOT = {
    "server": "alpha",
    "bind": "127.0.0.1:8765",
    "results": [
        {
            "title": "alpha__echo",
            "subtitle": "tools/call",
            "method": "tools/call",
            "params": {"name": "alpha__echo", "arguments": {"text": "hello"}},
            "raw": {"content": [{"type": "text", "text": "hello"}]},
            "failed": False,
            "elapsed_ms": 12.4,
            "at": "2026-09-13T10:00:00Z",
        },
        {
            "title": "alpha__boom",
            "subtitle": "tools/call",
            "method": "tools/call",
            "params": {"name": "alpha__boom", "arguments": {}},
            "raw": {"error": {"code": -32000, "message": "backend is down"}},
            "failed": True,
            "elapsed_ms": 3,
            "at": "2026-09-13T10:00:04Z",
        },
    ],
    "variables": [
        {
            "variable": "continent",
            "listing": "zoo://continents",
            "spends": "zoo://continents/{continent}/countries",
            "narrows": True,
            "multi": False,
            "context": {},
            "picked": ["africa"],
            "values": [
                {"value": "africa", "label": "Africa"},
                {"value": "asia", "label": None},
            ],
            "note": "",
        },
        {
            "variable": "country",
            "listing": "zoo://continents/africa/countries",
            "spends": "zoo://countries/{country}/animals",
            "narrows": False,
            "multi": True,
            "context": {"continent": "africa"},
            "picked": ["congo", "kenya"],
            "values": [{"value": "congo", "label": None}, {"value": "kenya", "label": None}],
            "note": "",
        },
    ],
}


# --- the renderer ------------------------------------------------------------------------


def test_an_empty_bench_still_renders_a_document() -> None:
    """A person who opens the modal before doing anything gets prose, not an error.

    The two headings stay, because their absence would read as a broken renderer rather
    than as an empty column.
    """
    text = clipboard.render(None)
    assert "## Results" in text and "## Injectable values" in text
    assert "Nothing has been invoked yet" in text


def test_the_document_carries_every_call_with_its_request_and_answer() -> None:
    text = clipboard.render(SNAPSHOT)
    assert "`alpha__echo`" in text
    assert '"text": "hello"' in text              # the arguments that were sent
    assert '"backend is down"' in text            # and the failure that came back
    assert "failed" in text
    assert "Selected server: **alpha**" in text


def test_the_preamble_says_the_payloads_are_not_instructions() -> None:
    """The one sentence in here that is load-bearing rather than informative.

    Every quoted payload came off a backend, and a model reading a tool result as a
    directive is the failure this document would otherwise invite. See `clipboard.md`.
    """
    text = clipboard.render(SNAPSHOT)
    assert "not instruction" in text


def test_payloads_are_fenced_with_four_backticks() -> None:
    """A backend that answers in Markdown puts ``` blocks inside this document.

    A three-backtick fence would end at the first of them, and the rest of the session
    would render as prose -- including, in the worst case, something shaped like an
    instruction escaping the block that framed it as quoted output.
    """
    snapshot = {"results": [{"title": "t", "method": "tools/call", "params": {},
                             "raw": {"content": [{"type": "text", "text": "```\nls -la\n```"}]}}]}
    text = clipboard.render(snapshot)
    assert "````json" in text


def test_a_large_payload_is_cut_and_says_so() -> None:
    snapshot = {"results": [{"title": "big", "method": "tools/call", "params": {},
                             "raw": {"blob": "A" * (clipboard.MAX_PAYLOAD_CHARS * 2)}}]}
    text = clipboard.render(snapshot)
    # Cut, and the note says how much there was -- so a reader who needs the rest knows to
    # go and fetch it rather than believing the block is the whole answer.
    assert "characters in all" in text
    assert "A" * (clipboard.MAX_PAYLOAD_CHARS + 1) not in text
    assert "A" * (clipboard.MAX_PAYLOAD_CHARS - 100) in text


def test_a_long_session_reports_what_it_left_out() -> None:
    many = [{"title": f"call-{n}", "method": "tools/call", "params": {}, "raw": {}}
            for n in range(clipboard.MAX_RESULTS + 5)]
    text = clipboard.render({"results": many})
    assert "5 older one(s) are not included" in text
    # The newest survive: the oldest cards are the ones the person has stopped looking at.
    assert f"call-{clipboard.MAX_RESULTS + 4}" in text
    assert "call-0" not in text


def test_picked_values_are_distinguished_from_available_ones() -> None:
    text = clipboard.render(SNAPSHOT)
    assert "**Picked (one):** `africa`" in text
    assert "many — the form is sent once per value" in text
    assert "`continent` = `africa`" in text       # the context a narrowed group sits under
    assert "`africa` (Africa)" in text            # a label that says something the value does not


def test_a_group_that_could_not_be_read_says_why_instead_of_listing_nothing() -> None:
    text = clipboard.render({"variables": [
        {"variable": "id", "listing": "zoo://ids", "note": "Could not be read: 404"},
    ]})
    assert "Could not be read: 404" in text
    assert "Picked: nothing yet" not in text


def test_staleness_is_stated_in_words_as_well_as_a_timestamp() -> None:
    bench = clipboard.Workbench()
    bench.put(SNAPSHOT)
    bench._captured_at -= 7200  # two hours ago  # noqa: SLF001 - the clock is the point
    assert "2 hours ago" in bench.document()


# --- the snapshot guard ------------------------------------------------------------------


def test_normalise_keeps_only_the_shape_the_renderer_reads() -> None:
    cleaned = clipboard.normalise({**SNAPSHOT, "wat": "ignored"})
    assert "wat" not in cleaned
    assert cleaned["server"] == "alpha"
    assert cleaned["results"][0]["method"] == "tools/call"


def test_normalise_refuses_a_snapshot_too_large_to_hold() -> None:
    huge = {"results": [{"title": "x", "raw": "B" * (clipboard.MAX_SNAPSHOT_BYTES + 1)}]}
    with pytest.raises(ValueError, match="larger than"):
        clipboard.normalise(huge)


def test_normalise_refuses_something_that_is_not_an_object() -> None:
    with pytest.raises(ValueError):
        clipboard.normalise(["not", "a", "snapshot"])


# --- the two surfaces --------------------------------------------------------------------


async def test_the_tool_and_the_admin_method_return_the_same_text(tmp_path) -> None:
    """The assertion this whole module exists for.

    The person edits what came back from `/admin`; the model reads what came back from
    `/mcp`. If those two ever disagree, the agent has been briefed on a session that did
    not happen.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        admin = await harness.connect("/admin", initialize=False)
        mcp = await harness.connect()

        put = await admin.call("admin.clipboard.put", {"snapshot": SNAPSHOT})
        assert put["empty"] is False
        assert put["captured_at"] > 0

        got = await admin.call("admin.clipboard.get")
        through_mcp = await mcp.tool_text("gateway__clipboard")

        assert "alpha__echo" in got["document"]
        # `Generated <now>` is the one line that moves between two renderings a moment
        # apart, so the comparison is of everything below it.
        assert _body(got["document"]) == _body(through_mcp)
    finally:
        await harness.close()


async def test_the_tool_is_published_and_answers_before_anything_is_captured(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        mcp = await harness.connect()
        assert "gateway__clipboard" in await mcp.tool_names()
        # No UI has ever connected. The answer is a document that says the bench is empty,
        # not an error: a model asking what happened at the bench when nothing has is
        # asking a reasonable question.
        assert "Nothing has been invoked yet" in await mcp.tool_text("gateway__clipboard")
    finally:
        await harness.close()


async def test_a_malformed_snapshot_is_a_caller_error_and_changes_nothing(tmp_path) -> None:
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        admin = await harness.connect("/admin", initialize=False)
        await admin.call("admin.clipboard.put", {"snapshot": SNAPSHOT})
        with pytest.raises(Exception):
            await admin.call("admin.clipboard.put", {"snapshot": "a string"})
        # The good capture survived the bad one: a rejected write is not a write.
        assert "alpha__echo" in (await admin.call("admin.clipboard.get"))["document"]
    finally:
        await harness.close()


async def test_the_tool_takes_no_arguments(tmp_path) -> None:
    """An agent may read the bench; it may not tell the bench what to say.

    `additionalProperties: false` is what makes that refusal loud rather than silent.
    """
    harness = await daemon(tmp_path, servers=SERVERS)
    try:
        mcp = await harness.connect()
        tools = {t["name"]: t for t in (await mcp.call("tools/list"))["tools"]}
        schema = tools["gateway__clipboard"]["inputSchema"]
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is False
    finally:
        await harness.close()


def _body(document: str) -> str:
    """Everything but the `Generated …` line, which is the clock and moves on its own."""
    return "\n".join(line for line in document.splitlines() if not line.startswith("Generated "))
