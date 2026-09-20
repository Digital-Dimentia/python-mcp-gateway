# `catalogue.py`

Aggregates every backend's tools, prompts and resources into one namespaced listing.

Process-wide and shared by every connection: the listing is a property of the backend pool,
not of who is asking.

## A sleeping backend keeps advertising

`_gather` walks every backend, not only the running ones, and `_fetch` consults the cache
before it looks at anything else. That is deliberate and it is what makes
[`supervisor.md`](supervisor.md)'s idle teardown invisible: a listing does not change by the
process going away, so the catalogue goes on answering from what the backend last said.

`running` alone was the right filter when a backend that is not running is a backend that is
broken. A slept one is neither broken nor gone, and dropping it here would make every
teardown look to a client like tools disappearing.

A **lazy** backend that has never run has no cache to answer from, so `_fetch` wakes it.
That is the one-time cost of `lazy`, and it is paid on the first listing rather than at
startup.

## Cached per backend, invalidated by `list_changed`

A `tools/list` that fanned out to twelve subprocesses on every call would make the cheapest
method in the protocol the most expensive, and clients call it often.

So each backend's listing is cached, and dropped when that backend emits `list_changed` or
is restarted — which is exactly what the notification is *for*. The cache is per backend
rather than global, so one backend changing its tools does not force a refetch from the
other eleven.

## A backend that fails to list is skipped, not fatal

A listing is a fan-out, and something in a fan-out will eventually be broken. One backend
timing out must not empty the catalogue: the model would see a gateway with nothing in it and
conclude the tools do not exist.

The failure is logged and recorded on the backend, where `gateway__backend_health` reports
it. That visibility is what makes skipping defensible rather than silent — the same trade as
skipping a backend with a missing credential.

## Names a client would reject are dropped

A composed name over 64 characters, or carrying a character real clients refuse, is left out
and recorded in `skipped_tools`.

Publishing it would hand the model a tool whose own client rejects the call — a protocol
error mid-turn that the model cannot act on — which is strictly worse than the tool not being
there. The health entry keeps the drop from being silent. See [`naming.md`](naming.md).

## `find_tool` looks up the backend, never the tool

Routing is `naming.split`, then a lookup of the **backend**. The tool name is handed to the
backend exactly as it gave it; whether it still exists is the backend's answer to give, not a
cache's to guess.

That is what keeps a call correct after a `list_changed` nobody has refetched yet — and it is
why the `__` separator rule in [`naming.md`](naming.md) has to hold, since there is no table
to fall back on.

## A tool's panel reference is namespaced with its name

A tool that ships a user interface carries `_meta.ui.resourceUri` naming a `ui://` resource
(MCP Apps, SEP-1865). That address means nothing in this gateway's listing, so
[`ui_apps.py`](ui_apps.md) rewrites it into the same `mcpgw://` space as the resource
listing, right beside the name composition it mirrors.

**The `ui://` authority is never consulted.** The reference is encoded under *the backend
that published the tool*, whatever the URI happens to say — which is the whole of what stops
one backend nominating another's interface. `ui_apps.md` has the argument; this note exists
so nobody adds the "obvious" resolution here.

A reference to a `ui://` the backend does not list is **published anyway** and recorded in
`unresolved_ui_templates` — the same visibility-instead-of-enforcement trade as
`skipped_tools`, for a check that reads a cache and therefore cannot be authoritative.
`_publishes` returns `True` when the listing is unknown, so "we have not looked" never reads
as "it is missing".

## Entries are copied, never mutated

The cached entry is the backend's own answer. A later reader — health, `/admin`, a refetch
comparison — must see it as the backend gave it, so namespacing builds a new dict rather than
rewriting the cached one in place.

This is why `rewrite_tool_meta` returns a rebuilt `_meta` rather than editing one: `dict()` is
a **shallow** copy, so reaching into `entry["_meta"]["ui"]` would write straight through to
the cache — and then rewrite the already-rewritten reference on the next call, encoding it
twice. A test asserting two consecutive listings are equal is what catches that.

## `counts` returns `None`, not `0`, for a backend nobody has listed

They are different facts. A health report that conflated them would say "this backend has no
tools" about one that has simply never been asked.

## The finders serve completions too

`find_prompt` and `find_resource` resolve a `completion/complete` `ref` as readily as they
resolve a `prompts/get` name or a `resources/read` URI, and that is the whole of what this
module had to do for the method: a ref *is* a name or a URI, in the same address space, and
a second resolver for it would be a second thing to keep in step. See
[`router.md`](router.md).
