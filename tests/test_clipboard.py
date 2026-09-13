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
    assert "text: hello" in text                  # the arguments that were sent
    assert "message: backend is down" in text     # and the failure that came back
    assert "failed" in text


def test_a_session_that_fits_says_nothing_about_its_own_length() -> None:
    """The count and the ordering are both plain from the numbered headings.

    A line is spent only on the thing the headings cannot show: that there were more calls
    than the document carries. `test_a_long_session_reports_what_it_left_out` is that case.
    """
    text = clipboard.render(SNAPSHOT)
    assert "oldest first" not in text.lower()
    assert "not included" not in text
    assert text.startswith("## Results\n\n### 1. ")


def test_the_document_opens_at_the_first_heading_and_says_nothing_about_itself() -> None:
    """No title, no `Generated …`, no paragraph explaining what a bench session is.

    Both readers pay for those lines and the model pays most: this lands in a context
    window the rest of its session still has to fit into. What the tool is and how its
    payloads should be read is in the tool's *description* -- read once at `tools/list`
    rather than re-paid on every call. See `clipboard.md`.
    """
    text = clipboard.render(SNAPSHOT)
    assert text.startswith("## Results")
    for gone in ("# MCP Gateway", "Generated ", "captured", "This is a transcript",
                 "Selected server", "Gateway: ", "session picked from them"):
        assert gone not in text, gone


def test_the_document_is_a_pure_function_of_the_snapshot() -> None:
    """No clock in it, so two renderings a second apart are the same bytes.

    Which is what lets `test_both_sockets_return_the_same_document` compare them whole
    rather than filtering a line out of each first.
    """
    assert clipboard.render(SNAPSHOT) == clipboard.render(SNAPSHOT)


def test_payloads_are_fenced_with_four_backticks() -> None:
    """A backend that answers in Markdown puts ``` blocks inside this document.

    A three-backtick fence would end at the first of them, and the rest of the session
    would render as prose -- including, in the worst case, something shaped like an
    instruction escaping the block that framed it as quoted output.
    """
    snapshot = {"results": [{"title": "t", "method": "tools/call", "params": {},
                             "raw": {"content": [{"type": "text", "text": "```\nls -la\n```"}]}}]}
    text = clipboard.render(snapshot)
    assert "````toon" in text


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
    assert "5 earlier call(s) are not included" in text
    # The newest survive: the oldest cards are the ones the person has stopped looking at.
    assert f"call-{clipboard.MAX_RESULTS + 4}" in text
    assert "call-0" not in text


def test_picked_values_are_distinguished_from_available_ones() -> None:
    text = clipboard.render(SNAPSHOT)
    assert "**Picked (one):** `africa`" in text
    assert "many — the form is sent once per value" in text
    assert "`continent` = `africa`" in text       # the context a narrowed group sits under
    assert "`africa` (Africa)" in text            # a label that says something the value does not


def test_payloads_are_written_as_toon_rather_than_json() -> None:
    """Same data, a third of the punctuation. See `toon.md` for why the reader is worth it.

    `tools/list` is the shape this pays off on: an array of uniform objects states its keys
    once as a header instead of once per element.
    """
    snapshot = {"results": [{"title": "t", "method": "tools/list", "params": {}, "raw": {
        "tools": [{"name": "alpha__echo", "description": "Echo"},
                  {"name": "alpha__boom", "description": "Fail"}]}}]}
    text = clipboard.render(snapshot)
    assert "tools[2]{name,description}:" in text
    assert "  alpha__echo,Echo" in text
    assert "{" not in text.split("Response:")[1].replace("{name,description}", "")


def test_a_prompt_carries_its_expanded_text_into_the_document() -> None:
    """The bench's three verbs all end up here, and this is the one with prose in it.

    The substitution is the backend's -- what a person got back from `prompts/get` is the
    filled-in message, not the template -- so the document hands an agent the text that was
    actually produced. `messages` is a uniform array, so it tabulates down to one row with
    a nested `content{type,text}` group.
    """
    snapshot = {"results": [{
        "title": "zoo-prompt-arguments", "method": "prompts/get",
        "params": {"name": "zoo-prompt-arguments", "arguments": {"subject": "the ocelot"}},
        "raw": {"description": "A prompt with its arguments substituted",
                "messages": [{"role": "user", "content": {
                    "type": "text", "text": "Write about the ocelot in a wry tone."}}]},
    }]}
    text = clipboard.render(snapshot)
    assert "messages[1]{role,content{type,text}}:" in text
    assert "user,text,Write about the ocelot in a wry tone." in text


def test_a_call_with_no_arguments_says_so_instead_of_fencing_an_empty_box() -> None:
    """TOON writes `{}` as an empty document, and an empty fence is a box to interpret."""
    text = clipboard.render({"results": [{"title": "t", "method": "tools/list",
                                          "params": {}, "raw": {}}]})
    assert "Request: no arguments." in text


def test_a_payload_that_is_already_prose_is_not_encoded() -> None:
    """Encoding it would quote a page of text and escape every newline in it."""
    text = clipboard.render({"results": [{"title": "t", "method": "tools/call", "params": {},
                                          "raw": "line one\nline two"}]})
    assert "line one\nline two" in text
    assert "\\n" not in text


def test_a_group_that_could_not_be_read_says_why_instead_of_listing_nothing() -> None:
    text = clipboard.render({"variables": [
        {"variable": "id", "listing": "zoo://ids", "note": "Could not be read: 404"},
    ]})
    assert "Could not be read: 404" in text
    assert "Picked: nothing yet" not in text


def test_when_the_bench_was_captured_is_reported_beside_the_document_not_inside_it() -> None:
    """Staleness is metadata, and metadata does not belong in the model's context.

    `admin.clipboard.get` hands the page a number it can say what it likes about; the
    document stays evidence about the calls.
    """
    bench = clipboard.Workbench()
    bench.put(SNAPSHOT)
    assert bench.captured_at > 0
    assert "ago" not in bench.document()


# --- the snapshot guard ------------------------------------------------------------------


def test_normalise_keeps_only_the_shape_the_renderer_reads() -> None:
    cleaned = clipboard.normalise({**SNAPSHOT, "wat": "ignored"})
    assert "wat" not in cleaned
    assert cleaned["results"][0]["method"] == "tools/call"
    # Dropped along with the preamble that was the only thing rendering them.
    assert "server" not in cleaned and "bind" not in cleaned


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
        # Whole documents, byte for byte. There is no clock in the renderer any more, so
        # there is nothing to filter out before comparing -- which is a stronger assertion
        # than the one this used to make.
        assert got["document"] == through_mcp
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
