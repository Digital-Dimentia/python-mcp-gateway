# `naming.py`

Every public name the gateway exposes, composed and split. Pure functions, no I/O.

## The coupling that makes routing work

Two rules, in two different modules, that only work as a pair:

1. `validate_server_name` refuses a server name containing `__`.
2. Routing is `public.partition("__")` — a split on the *first* occurrence.

Because of (1), the first `__` in a public name is always the separator. That is what lets
(2) be a pure string operation with **no lookup table** — and a table is exactly what you
would otherwise need, kept in sync with every backend's current tool list, going stale the
moment a backend emits `tools/list_changed` and renames something.

It also means a backend tool that already contains the separator needs no escaping:
`github__create__issue` splits cleanly into `github` and `create__issue`.

Break either rule and the other misroutes silently — a call lands on the wrong backend, or
on no backend, with no error to point at the cause. `tests/test_naming.py` asserts both
halves, together.

## `gateway` is reserved

The gateway's own meta-tools are `gateway__list_backends` and friends. A backend named
`gateway` could publish a tool whose composed name collides with one of them, handing the
model a `gateway__reload_config` of the backend's devising. `config.py` refuses the name at
load, which is the only place a server name is chosen.

## Publishable names

`is_publishable` checks `^[a-zA-Z0-9_-]{1,64}$`. The MCP spec does not impose this;
real clients do. A composed name that fails it is **dropped from the listing** with a
warning and recorded in `skipped_tools`, rather than published — offering the model a tool
whose name its own client will reject produces a protocol error mid-turn that the model
cannot act on, which is strictly worse than the tool not being there.

The warning and the health-tool entry are what keep the drop from being silent; without
them this would be the wrong trade.

## Resource URIs

`mcpgw://{server}/{percent-encoded original}`.

**Why rewrite rather than pass the original through and search for it at read time.** Two
backends can publish the identical URI — two filesystem servers rooted at different
directories both offering `file:///README.md` — and then which one answers a
`resources/read` is decided by dictionary ordering. Rewriting makes every address
unambiguous by construction. The cost is a URI the client cannot interpret on its own,
which is nominal: clients treat resource URIs as opaque handles.

`quote(uri, safe="{}")` leaves braces unescaped so that RFC 6570 expressions in a
`uriTemplate` survive — `file:///{path}` stays expandable by the client. A concrete URI
containing a literal `{` becomes indistinguishable from a template expression; that is
**declared unsupported** rather than worked around, because the only fix would be a second
escaping layer inside the percent-encoding and nobody publishes such a URI.

`compose_display_name` uses `server/name`, not the `__` form: that string is read by a
person in a picker and never split by code, and a slash is what a person reads as "from".
