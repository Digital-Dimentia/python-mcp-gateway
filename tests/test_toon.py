"""The TOON encoder, against the shapes an MCP payload actually takes.

The bar here is not compactness, it is that a **strict** decoder reads back exactly the
JSON that went in. A document a decoder rejects is worse than the JSON it replaced: the
model cannot tell a format it mis-parsed from a bench that did something strange. So most
of what follows is about the cases where the format is ambiguous unless the encoder is
careful -- an empty object against an empty array, a string that looks like a number, an
array of objects that is one key away from being a table.

Spec: `toon-format/spec`, SPEC.md. See `src/mcp_gateway/toon.md` for why this exists.
"""

from __future__ import annotations

import pytest

from mcp_gateway.toon import encode

# --- the shapes MCP answers with ---------------------------------------------------------


def test_a_uniform_array_of_objects_states_its_keys_once() -> None:
    """The saving this module exists for. `tools/list` is exactly this shape."""
    payload = {"tools": [{"name": "zoo__feed", "description": "Feed one"},
                         {"name": "zoo__list", "description": "List them"}]}
    assert encode(payload) == (
        "tools[2]{name,description}:\n"
        "  zoo__feed,Feed one\n"
        "  zoo__list,List them"
    )


def test_a_tool_result_encodes_as_its_content_rows_and_its_flag() -> None:
    payload = {"content": [{"type": "text", "text": "hello"}], "isError": False}
    assert encode(payload) == 'content[1]{type,text}:\n  text,hello\nisError: false'


def test_nested_objects_indent_by_two() -> None:
    payload = {"error": {"code": -32000, "message": "backend is down"}}
    assert encode(payload) == "error:\n  code: -32000\n  message: backend is down"


def test_a_nested_uniform_column_becomes_a_field_group() -> None:
    payload = {"rows": [{"id": 1, "at": {"h": 10, "m": 0}},
                        {"id": 2, "at": {"h": 11, "m": 30}}]}
    assert encode(payload) == (
        "rows[2]{id,at{h,m}}:\n"
        "  1,10,0\n"
        "  2,11,30"
    )


def test_an_array_of_primitives_stays_on_one_line() -> None:
    assert encode({"tags": ["admin", "ops"]}) == "tags[2]: admin,ops"


# --- where the encoder gives up compactness to stay decodable -----------------------------


def test_an_array_whose_objects_differ_goes_to_list_form_entire() -> None:
    """One missing key and the table is off, for the whole array.

    A table with a ragged row is a table a strict decoder counts wrongly, and half a table
    is not a thing the format has.
    """
    payload = {"items": [{"a": 1, "b": 2}, {"a": 3}]}
    assert encode(payload) == "items[2]:\n  - a: 1\n    b: 2\n  - a: 3"


@pytest.mark.parametrize("column", [
    [{"a": [1]}, {"a": [2]}],      # an array in a cell
    [{"a": {}}, {"a": {}}],        # an empty object in a cell
    [{"a": 1}, {"a": {"b": 2}}],   # a column that is a primitive in one row and not another
])
def test_a_column_that_is_not_uniform_disqualifies_the_table(column: list) -> None:
    assert not encode({"items": column}).startswith("items[2]{")


def test_an_empty_object_and_an_empty_array_are_spelled_differently() -> None:
    """The one distinction a careless encoder loses, and the payload it loses it on.

    `{"arguments": {}}` and `{"arguments": []}` are different answers, and a decoder given
    one spelling for both cannot say which the backend returned.
    """
    assert encode({"arguments": {}}) == "arguments:"
    assert encode({"arguments": []}) == "arguments: []"


@pytest.mark.parametrize("text", ["42", "1e-6", "true", "false", "null", "", " padded ",
                                  "-leading", "#hash", "has,comma", "has:colon",
                                  'has"quote', "has[bracket]"])
def test_a_string_that_would_decode_as_something_else_is_quoted(text: str) -> None:
    assert encode({"v": text}).startswith('v: "')


@pytest.mark.parametrize("text", ["plain", "with space", "zoo__feed", "Feed one animal"])
def test_a_string_that_needs_no_quotes_does_not_get_them(text: str) -> None:
    assert encode({"v": text}) == f"v: {text}"


def test_a_uri_is_quoted_because_it_carries_a_colon() -> None:
    """Worth its own case: this document is full of them.

    A value and its key separate at the first unquoted colon, so `mcpgw://a` unquoted would
    decode as something else entirely -- the spec quotes on the character, not on whether
    this particular one would have been ambiguous in this particular position.
    """
    assert encode({"uri": "mcpgw://a/b"}) == 'uri: "mcpgw://a/b"'


def test_control_characters_are_escaped() -> None:
    assert encode({"v": "a\nb\tc\\d\"e\x00f"}) == 'v: "a\\nb\\tc\\\\d\\"e\\u0000f"'


def test_a_key_that_is_not_an_identifier_is_quoted() -> None:
    assert encode({"not a key": 1}) == '"not a key": 1'
    assert encode({"dotted.key": 1}) == "dotted.key: 1"


# --- scalars ------------------------------------------------------------------------------


def test_booleans_are_not_rendered_as_the_integers_python_says_they_are() -> None:
    """`True` is an `int` in Python, and a rendering that said `1` would change the answer."""
    assert encode({"a": True, "b": False, "c": 1}) == "a: true\nb: false\nc: 1"


@pytest.mark.parametrize(("value", "expected"), [
    (1.0, "1"), (1.5, "1.5"), (-0.0, "0"), (1e21, "1e+21"),
    (float("nan"), "null"), (float("inf"), "null"), (None, "null"),
])
def test_numbers_are_canonical_and_the_ones_json_cannot_hold_are_null(value, expected) -> None:
    assert encode({"v": value}) == f"v: {expected}"


# --- roots --------------------------------------------------------------------------------


def test_the_root_can_be_an_array_a_primitive_or_nothing() -> None:
    assert encode([{"a": 1}, {"a": 2}]) == "[2]{a}:\n  1\n  2"
    assert encode([1, 2]) == "[2]: 1,2"
    assert encode([]) == "[]"
    assert encode("bare") == "bare"
    # An empty document, which is what a strict decoder reads back as `{}`.
    assert encode({}) == ""


def test_a_list_item_carrying_a_table_puts_the_header_on_the_hyphen_line() -> None:
    """The layout from SPEC.md's own example: rows two levels in from the hyphen."""
    payload = {"items": [{"users": [{"id": 1, "name": "Ada"}], "status": "active"}, 2]}
    assert encode(payload) == (
        "items[2]:\n"
        "  - users[1]{id,name}:\n"
        "      1,Ada\n"
        "    status: active\n"
        "  - 2"
    )
