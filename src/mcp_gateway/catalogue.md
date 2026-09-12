# `catalogue.py`

Aggregates every backend's tools, prompts and resources into one namespaced listing.

Process-wide and shared by every connection: the listing is a property of the backend pool,
not of who is asking.

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

## Entries are copied, never mutated

The cached entry is the backend's own answer. A later reader — health, `/admin`, a refetch
comparison — must see it as the backend gave it, so namespacing builds a new dict rather than
rewriting the cached one in place.

## `counts` returns `None`, not `0`, for a backend nobody has listed

They are different facts. A health report that conflated them would say "this backend has no
tools" about one that has simply never been asked.

## The finders serve completions too

`find_prompt` and `find_resource` resolve a `completion/complete` `ref` as readily as they
resolve a `prompts/get` name or a `resources/read` URI, and that is the whole of what this
module had to do for the method: a ref *is* a name or a URI, in the same address space, and
a second resolver for it would be a second thing to keep in step. See
[`router.md`](router.md).
