# `toon.py`

The payloads in the bench briefing, written as TOON instead of JSON.

[TOON](https://github.com/toon-format/spec) — Token-Oriented Object Notation — encodes the
JSON data model in a line-oriented, indentation-based form. It is the same data; what it
drops is the punctuation and the repetition.

```
                                        tools[2]{name,description}:
{"tools": [                               zoo__feed,Feed one animal
  {"name": "zoo__feed",                   zoo__list,List the animals
   "description": "Feed one animal"},
  {"name": "zoo__list",
   "description": "List the animals"}]}
```

## Why the briefing is written this way

The reader of `gateway__clipboard` is a model, and the document lands in a context window
that the rest of its session still has to fit into. A pretty-printed MCP result spends most
of its lines on a brace, a bracket, or a key it has already stated — and every one of those
is paid for twice, once to read and once in the room the model no longer has afterwards.
This is the same argument that removed the preamble; see [`clipboard.md`](clipboard.md).

The saving is largest exactly where MCP payloads are largest: `tools/list`, `resources/list`
and `prompts/list` all answer with a uniform array of objects, which is the shape TOON
states once as a header and then fills in a row at a time.

## What is here and what is not

**Encoder only.** Nothing in this gateway reads TOON back. The document flows one way — a
snapshot the page posted, a model that reads prose — and a decoder would be a second
implementation of a format with nothing to check it against. Every wire frame, `servers.yaml`
and every JSON-RPC payload stay JSON; this module is reached for in exactly one place,
`clipboard._fence`.

**A bare string payload is not encoded.** A tool that answered with a page of prose is
fenced as that prose. Running it through the encoder would wrap it in quotes and escape
every newline in it, which is a rendering that costs tokens to make the text harder to read.

**Conformant for the shapes a briefing can hold**, per `toon-format/spec`: objects, tabular
arrays with nested field groups, list-form arrays, inline primitive arrays, and the quoting,
escaping and number rules. Deliberately absent are the tab and pipe delimiters (comma is the
default and the only one emitted), key folding, and the keyed tabular form for
objects-of-objects — a form nothing emits is a form no test would be honest about.

## The rule that decides everything

**Strictly decodable, first.** A document a strict decoder rejects is worse than the JSON it
replaced: the reader cannot tell a format it mis-parsed from a bench that did something
strange. So where compactness and correctness disagree, this gives up the compactness —
an array whose objects differ by one key goes to list form entire rather than a table with
a ragged row, and a string that could be read back as a number, a boolean or `null` is
quoted even when the quotes look unnecessary.

The distinction between an empty object and an empty array is part of that: `key:` is the
empty object and `key: []` is the empty array, because a decoder given one spelling for both
cannot say which the backend returned.
