"""TOON: the JSON data model, written for a model to read rather than a parser to.

Every payload the clipboard briefing quotes used to be `json.dumps(indent=2)`, and a
pretty-printed MCP result spends most of its lines on punctuation -- a brace, a bracket and
a repeated key per element. The reader is a model with a context window the rest of its
session still has to fit into, so those lines are paid for twice: once to write and once in
everything the model does afterwards with less room. TOON encodes the same data with the
structure stated once:

    content[2]{type,text}:
      text,hello
      text,"and, quoted"

against the JSON

    {"content": [{"type": "text", "text": "hello"},
                 {"type": "text", "text": "and, quoted"}]}

## What this is not

It is an **encoder only**. Nothing in this gateway reads TOON back: the document flows one
way, from a snapshot the page posted to a model that reads prose, and a decoder would be a
second implementation of a format we would then have to keep two views of in step. The
wire, the config and every JSON-RPC frame stay JSON, which is not a thing this module has an
opinion about -- it is reached for in exactly one place, `clipboard._fence`.

## Conformance, and where it stops

This follows the TOON spec (`toon-format/spec`, SPEC.md) for the parts the briefing can
produce: objects, tabular arrays including nested field groups, list-form arrays, inline
primitive arrays, and the quoting, escaping and number rules. What it does not implement is
the delimiter options (comma only, which is the default), key folding (an optional encoder
convenience) and the keyed tabular form for objects-of-objects -- an MCP payload is arrays
of objects, and a form nothing emits is a form no test would be honest about.

The output is `strict`-decodable. That matters more than compactness: a document a strict
decoder rejects is worse than the JSON it replaced, because the reader cannot tell a format
it mis-parsed from a bench that did something strange.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: Two spaces per level. The spec fixes this -- tabs are not allowed as indentation -- and
#: it is the one part of the layout a reader's eye depends on.
INDENT = "  "

#: The default delimiter, and the only one this encoder emits. Tab and pipe are the others.
DELIMITER = ","

#: A key that needs no quotes. Anything else is quoted and escaped like a string.
BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

#: A string that would come back as a number. Quoted so it stays a string: `"42"` and `42`
#: are different values, and a briefing that blurred them would be lying about a payload.
LOOKS_NUMERIC = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

#: Characters that force quotes wherever they appear in a value, per the spec: the
#: structural ones, the delimiter, and anything below U+0020.
FORCES_QUOTES = set(':"\\[]{}' + DELIMITER)

#: The words a bare string would be decoded as something other than a string.
RESERVED = {"true", "false", "null"}

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def encode(value: Any) -> str:
    """`value` as a TOON document. No trailing newline, per the spec.

    An empty object encodes as the empty document, which is what a strict decoder reads it
    back as. Callers that need an empty payload to *look* like something on the page are
    the ones that know what it should say; see `clipboard._one_result`.
    """
    if isinstance(value, dict):
        return "\n".join(_object_lines(value, 0))
    if isinstance(value, list):
        return "\n".join(_array_lines(None, value, 0))
    return _scalar(value)


# --- objects ------------------------------------------------------------------------------


def _object_lines(obj: dict, depth: int) -> list[str]:
    lines: list[str] = []
    for key, value in obj.items():
        lines += _field_lines(_key(key), value, depth)
    return lines


def _field_lines(key: str, value: Any, depth: int) -> list[str]:
    """One `key: …` field, however many lines that takes."""
    pad = INDENT * depth
    if isinstance(value, dict):
        # A bare `key:` decodes as an empty object, which is exactly what this is. The
        # explicit `key: []` below is the empty *array*, and the spec keeps them apart
        # because a decoder otherwise cannot tell which was meant.
        if not value:
            return [f"{pad}{key}:"]
        return [f"{pad}{key}:", *_object_lines(value, depth + 1)]
    if isinstance(value, list):
        return _array_lines(key, value, depth)
    return [f"{pad}{key}: {_scalar(value)}"]


# --- arrays -------------------------------------------------------------------------------


def _array_lines(key: str | None, items: list, depth: int) -> list[str]:
    """An array in whichever of the three forms its contents allow.

    `key` is `None` for a root array, whose header carries no name.
    """
    pad = INDENT * depth
    head = key or ""

    if not items:
        return [f"{pad}{head}: []"] if key else [f"{pad}[]"]

    if all(_is_primitive(item) for item in items):
        row = DELIMITER.join(_scalar(item) for item in items)
        return [f"{pad}{head}[{len(items)}]: {row}"]

    fields = _tabular_fields(items)
    if fields is not None:
        header = DELIMITER.join(_field_group(name, sub) for _, name, sub in fields)
        rows = [f"{INDENT * (depth + 1)}{_row(item, fields)}" for item in items]
        return [f"{pad}{head}[{len(items)}]{{{header}}}:", *rows]

    lines = [f"{pad}{head}[{len(items)}]:"]
    for item in items:
        lines += _item_lines(item, depth + 1)
    return lines


def _item_lines(item: Any, depth: int) -> list[str]:
    """One `- ` entry of a list-form array.

    An object's first field goes on the hyphen line and the rest align under it, which is
    what puts a nested array's rows two levels in from the hyphen.
    """
    pad = INDENT * depth
    if _is_primitive(item):
        return [f"{pad}- {_scalar(item)}"]
    if isinstance(item, list):
        inner = _array_lines(None, item, depth + 1)
        return [f"{pad}- {inner[0].lstrip()}", *inner[1:]]
    if not item:
        # `{}` as an entry. A bare hyphen would decode as null, so the empty object is
        # spelled the way §9.4 spells it.
        return [f"{pad}-"]

    lines: list[str] = []
    for index, (key, value) in enumerate(item.items()):
        field = _field_lines(_key(key), value, depth + 1)
        if index == 0:
            lines += [f"{pad}- {field[0].lstrip()}", *field[1:]]
        else:
            lines += field
    return lines


def _tabular_fields(items: list) -> list[tuple[Any, str, list | None]] | None:
    """The header's field list, or `None` if this array is not tabular.

    Tabular needs every element to be a non-empty object with the same key set, and every
    column to be uniform: all primitives, or all non-empty objects that are themselves
    uniform, recursively. Anything else -- one array in one cell, one missing key, one
    `{}` -- and the whole array goes to list form, because a table with a ragged row is a
    table a decoder counts wrongly.
    """
    if not all(isinstance(item, dict) and item for item in items):
        return None
    keys = list(items[0])
    if any(set(item) != set(keys) for item in items):
        return None

    # Each field carries the key it was read from as well as the name it is written as:
    # a row is looked up by the former and printed with the latter, and decoding an
    # encoded key back into a lookup would be a round trip with nothing to gain.
    fields: list[tuple[Any, str, list | None]] = []
    for key in keys:
        column = [item[key] for item in items]
        if all(_is_primitive(value) for value in column):
            fields.append((key, _key(key), None))
            continue
        nested = _tabular_fields(column)
        if nested is None:
            return None
        fields.append((key, _key(key), nested))
    return fields


def _field_group(name: str, sub: list | None) -> str:
    """`name`, or `name{a,b}` when the column is a nested group."""
    if sub is None:
        return name
    return f"{name}{{{DELIMITER.join(_field_group(n, s) for _, n, s in sub)}}}"


def _row(item: dict, fields: list[tuple[Any, str, list | None]]) -> str:
    """One tabular row: cells in header order, nested groups flattened depth-first."""
    cells = [_scalar(item[key]) if sub is None else _row(item[key], sub)
             for key, _, sub in fields]
    return DELIMITER.join(cells)


# --- scalars ------------------------------------------------------------------------------


def _is_primitive(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    # Before the `int` branch: `True` is an `int` in Python, and a payload whose booleans
    # came back as `1` would be a rendering that changed the answer.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    return _string(value if isinstance(value, str) else str(value))


def _number(value: float) -> str:
    """Canonical decimal, and `null` for the values JSON cannot hold anyway."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    return text.replace("e-0", "e-").replace("e+0", "e+") if "e" in text else text


def _string(text: str) -> str:
    if _needs_quotes(text):
        return '"' + "".join(_escape(ch) for ch in text) + '"'
    return text


def _needs_quotes(text: str) -> bool:
    if not text or text != text.strip():
        return True
    if text in RESERVED or LOOKS_NUMERIC.match(text):
        return True
    if text[0] in "-#":
        return True
    return any(ch in FORCES_QUOTES or ord(ch) < 0x20 for ch in text)


def _escape(ch: str) -> str:
    if ch in _ESCAPES:
        return _ESCAPES[ch]
    return f"\\u{ord(ch):04x}" if ord(ch) < 0x20 else ch


def _key(key: Any) -> str:
    text = key if isinstance(key, str) else str(key)
    return text if BARE_KEY.match(text) else _string_forced(text)


def _string_forced(text: str) -> str:
    return '"' + "".join(_escape(ch) for ch in text) + '"'

