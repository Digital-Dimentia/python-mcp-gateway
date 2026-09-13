"""The session briefing: what the bench did, written for a model to read.

The admin UI is a place a person exercises a backend by hand -- press Send, look at what
came back, pick a value, press Send again. At the end of that there is something worth
handing to an agent, and until now no way to hand it over: the results live as DOM nodes in
a column, and the values picked for the next call live in a `Map` in `app.js`.

This module is the document that carries them. One renderer, two surfaces, exactly as
`admin.md` describes for the meta-tools:

- **The Clipboard modal** on `/admin`, where the text lands in a textarea, is edited if the
  person wants to trim or annotate it, and is copied.
- **`gateway__clipboard` on `/mcp`**, where the same text is fetched by the model itself,
  unedited, so "read what I just did at the bench" is a tool call rather than a paste.

## Why the browser does not write it

The page has the state; the gateway has the model attached. If the page rendered the
document the tool would need a second renderer, and two renderers of one document drift --
which would show up as an agent being told something subtly different from what the person
copied. So the page publishes a *snapshot* -- results and picks as data -- and this module
is the only thing that turns a snapshot into prose.

The snapshot is process-wide and there is exactly one, the most recent. Not per connection:
the point is that the model on `/mcp` reads what the person did on `/admin`, and those are
two different sockets. When it was captured is reported *beside* the document by
`admin.clipboard.get`, never inside it -- see the first rule below.

## The four rules in the document itself

**Nothing is spent on prose about the document.** It opens at `## Results` and goes straight
to the first call -- no title, no "generated at", no paragraph explaining what a bench
session is, no count of the calls, no note introducing the values. Both readers pay for those
lines, and the one that pays most is the model: `gateway__clipboard` lands in a context
window that the rest of the session still has to fit into, and a briefing that spends its
first page introducing itself makes every later turn slightly worse. When the capture
happened is the caller's business -- the tool answers now, and `admin.clipboard.get` reports
`captured_at` beside the document rather than inside it.

**Payloads are TOON, not JSON.** The same data with the punctuation left out and a uniform
array's keys stated once rather than per element -- see `toon.md`, which also says why the
encoder gives up compactness whenever it would cost strict decodability.

**Backend output is quoted, never adopted.** Everything under Results came off a backend
this gateway happens to have launched, and a model reading it is one prompt injection away
from taking a tool result as an instruction. Every payload is fenced, and the warning itself
lives in the tool's *description* in `admin.py` -- read once when tools are listed, rather
than re-paid on every call the way a line in the document would be.

**Nothing is silently dropped.** A response too large to include is reported as truncated,
with its real size; a snapshot holding more results than the document will carry says how
many it left out. A briefing that quietly loses half a session is worse than no briefing,
because nothing on the page says it happened.
"""

from __future__ import annotations

import json
import time
from typing import Any

from mcp_gateway import toon

#: How much of one response body the document carries before it is cut. Generous: the
#: reader is a model with a large window, and a tool result cut off mid-structure is worth
#: less than the bytes it saved. A base64 image is what this is actually for.
MAX_PAYLOAD_CHARS = 8000

#: How many result cards the document carries, newest last. The column is append-only and a
#: long session runs to dozens; the oldest are the ones the person has stopped looking at.
MAX_RESULTS = 40

#: How many values one vocabulary lists before the rest are counted rather than named.
MAX_VALUES = 60

#: What a snapshot may be at all, in bytes of JSON. A guard on the `/admin` method rather
#: than a budget: the page is trusted, but it is also the one thing here that can be made to
#: post an arbitrary payload, and a gateway that holds a hundred megabytes because a tool
#: returned one is a gateway that fell over for a silly reason.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) and value else default


def _fence(payload: Any, *, language: str = "toon") -> list[str]:
    """One payload as a fenced block, cut at `MAX_PAYLOAD_CHARS` and saying so if cut.

    Structure is written as TOON rather than JSON -- same data, a third of the punctuation,
    and the keys of a uniform array stated once instead of per element. `toon.py` says why
    that is worth a format the reader may have to think about for a moment. A payload that
    is already a bare string is fenced as it stands: encoding it would wrap a page of prose
    in quotes and escape every newline in it.

    The fence is four backticks rather than three: a tool that answers in Markdown -- and
    plenty do -- puts three-backtick blocks inside this one, and a three-backtick fence
    would end at the first of them and leave the rest of the document reading as prose.
    """
    if isinstance(payload, str):
        body = payload
        language = ""
    else:
        try:
            body = toon.encode(payload)
        except (TypeError, ValueError):  # pragma: no cover - the encoder takes everything
            body = str(payload)

    note = None
    if len(body) > MAX_PAYLOAD_CHARS:
        note = f"_…truncated: {len(body):,} characters in all, {MAX_PAYLOAD_CHARS:,} shown._"
        body = body[:MAX_PAYLOAD_CHARS]

    lines = [f"````{language}".rstrip(), body, "````"]
    if note:
        lines.append(note)
    return lines


class Workbench:
    """The most recent snapshot of the admin UI's two right-hand columns.

    Process-wide and single. See the module docstring for why it is not per connection.
    """

    def __init__(self) -> None:
        self._snapshot: dict[str, Any] | None = None
        self._captured_at: float | None = None

    def put(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._captured_at = time.time()

    def clear(self) -> None:
        self._snapshot = None
        self._captured_at = None

    @property
    def captured_at(self) -> float | None:
        return self._captured_at

    def snapshot(self) -> dict[str, Any] | None:
        return self._snapshot

    def document(self) -> str:
        """The briefing, rendered from whatever was published last.

        `captured_at` is kept here and reported alongside the document by
        `admin.clipboard.get`, not written into it -- see the module docstring.
        """
        return render(self._snapshot)


def render(snapshot: dict[str, Any] | None) -> str:
    """The whole document, as Markdown.

    Pure, and a pure function of the snapshot alone -- no clock, so two renderings of one
    bench are byte-identical. The same snapshot renders the same text on both surfaces,
    which is the entire reason this lives in Python rather than in `app.js`.

    It opens at `## Results`. There is no preamble and no title: every line here is spent
    out of the reading model's context window, and prose about the document is not
    evidence about the bench. See `clipboard.md`.
    """
    lines: list[str] = []
    lines += _results_section(_as_list((snapshot or {}).get("results")))
    lines += _variables_section(_as_list((snapshot or {}).get("variables")))
    return "\n".join(lines).rstrip() + "\n"


def _results_section(results: list[Any]) -> list[str]:
    lines = ["## Results", ""]
    if not results:
        lines += [
            "Nothing has been invoked yet — the Results column is empty.",
            "",
        ]
        return lines

    shown = results[-MAX_RESULTS:]
    dropped = len(results) - len(shown)
    # Only when something was actually cut. The count and the ordering are both plain from
    # the numbered headings below; what is *not* plain from them is a session that had more
    # calls in it than this document carries, so that is the only case worth a line.
    if dropped:
        lines += [f"Oldest first. {dropped} earlier call(s) are not included.", ""]

    for index, entry in enumerate(shown, start=1):
        lines += _one_result(index, entry if isinstance(entry, dict) else {})
    return lines


def _one_result(index: int, entry: dict[str, Any]) -> list[str]:
    title = _text(entry.get("title"), "(untitled)")
    method = _text(entry.get("method"), _text(entry.get("subtitle"), "call"))
    failed = bool(entry.get("failed"))

    facts = []
    elapsed = entry.get("elapsed_ms")
    if isinstance(elapsed, (int, float)):
        facts.append(f"{round(elapsed)} ms")
    at = _text(entry.get("at"))
    if at:
        facts.append(at)
    # The word, not a tick: `failed` here covers both a JSON-RPC error and a result carrying
    # `isError`, and the payload below says which. See `render.js` on that distinction.
    facts.append("failed" if failed else "ok")

    lines = [f"### {index}. `{title}` — `{method}`", "", " · ".join(facts), ""]
    params = entry.get("params")
    # TOON writes an empty object as an empty document, which in a fence would be a blank
    # box the reader has to interpret. A call with no arguments says so in three words.
    if params:
        lines += ["Request:", *_fence(params)]
    else:
        lines.append("Request: no arguments.")
    lines += ["", "Failed with:" if failed else "Response:"]
    lines += _fence(entry.get("raw"))
    lines.append("")
    return lines


def _variables_section(groups: list[Any]) -> list[str]:
    lines = ["## Injectable values", ""]
    if not groups:
        lines += [
            "No vocabularies are open. (The right-hand column offers the values a server "
            "publishes for its own template parameters; none were opened in this session.)",
            "",
        ]
        return lines

    for group in groups:
        lines += _one_group(group if isinstance(group, dict) else {})
    return lines


def _one_group(group: dict[str, Any]) -> list[str]:
    variable = _text(group.get("variable"), "(unnamed)")
    listing = _text(group.get("listing"))
    spends = _text(group.get("spends"))

    head = f"### `{variable}`"
    lines = [head, ""]
    if listing:
        lines.append(f"- Published by: `{listing}`")
    if spends:
        lines.append(f"- {'Narrows' if group.get('narrows') else 'Spent on'}: `{spends}`")

    context = group.get("context")
    if isinstance(context, dict) and context:
        bound = " · ".join(f"`{name}` = `{value}`" for name, value in context.items())
        lines.append(f"- Under: {bound}")

    note = _text(group.get("note"))
    if note:
        lines += ["", note, ""]
        return lines

    picked = [str(v) for v in _as_list(group.get("picked"))]
    if picked:
        mode = "many — the form is sent once per value" if group.get("multi") else "one"
        lines.append(f"- **Picked ({mode}):** {', '.join(f'`{v}`' for v in picked)}")
    else:
        lines.append("- Picked: nothing yet")

    values = [v for v in _as_list(group.get("values")) if isinstance(v, dict)]
    if values:
        shown = values[:MAX_VALUES]
        rest = len(values) - len(shown)
        rendered = ", ".join(_one_value(v) for v in shown)
        tail = f", …and {rest} more" if rest else ""
        lines.append(f"- Available ({len(values)}): {rendered}{tail}")
    lines.append("")
    return lines


def _one_value(value: dict[str, Any]) -> str:
    text = f"`{value.get('value')}`"
    label = _text(value.get("label"))
    return f"{text} ({label})" if label else text


def normalise(snapshot: Any) -> dict[str, Any]:
    """Take what the page posted and keep only the shape this module renders.

    Not validation for its own sake: it is what stops a future `app.js` from putting a key
    here that quietly never appears, and it is where the size guard lives.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("a clipboard snapshot must be an object")

    encoded = json.dumps(snapshot, default=str)
    if len(encoded.encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError(
            f"snapshot is larger than {MAX_SNAPSHOT_BYTES} bytes; the page should trim its "
            f"payloads before publishing"
        )

    # No `server` or `bind`: the document does not name either, and a key kept here that
    # nothing renders is exactly the quiet dead weight this function exists to prevent. The
    # server is in every entry anyway -- tool names are `server__tool`.
    return {
        "results": [_clean_result(r) for r in _as_list(snapshot.get("results")) if isinstance(r, dict)],
        "variables": [
            _clean_group(g) for g in _as_list(snapshot.get("variables")) if isinstance(g, dict)
        ],
    }


def _clean_result(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _text(entry.get("title")),
        "subtitle": _text(entry.get("subtitle")),
        "method": _text(entry.get("method")),
        "params": entry.get("params"),
        "raw": entry.get("raw"),
        "failed": bool(entry.get("failed")),
        "elapsed_ms": entry.get("elapsed_ms") if isinstance(entry.get("elapsed_ms"), (int, float)) else None,
        "at": _text(entry.get("at")),
    }


def _clean_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "variable": _text(group.get("variable")),
        "listing": _text(group.get("listing")),
        "spends": _text(group.get("spends")),
        "narrows": bool(group.get("narrows")),
        "multi": bool(group.get("multi")),
        "context": group.get("context") if isinstance(group.get("context"), dict) else {},
        "picked": [str(v) for v in _as_list(group.get("picked"))],
        "values": [
            {"value": str(v.get("value")), "label": _text(v.get("label")) or None}
            for v in _as_list(group.get("values"))
            if isinstance(v, dict)
        ],
        "note": _text(group.get("note")),
    }
