# `visibility.py`

Which of a backend's tools the MCP port advertises, held in memory for as long as the daemon
runs.

Six backends can hand a model two hundred tools, and a tool it will never call still costs
context on every turn. The only lever before this was `enabled: false`, which is the whole
server and stops the subprocess — no use when you need two of a server's fifteen tools.

## Hiding is context economy, not access control

**`tools/call` is not filtered.** Routing stays the pure string split with no lookup table
([`naming.md`](naming.md), [`router.md`](router.md)), so every hidden tool remains callable by
any client that knows its public name.

That is the decision the rest of this module rests on, and it is what makes the bench
exemption below acceptable: a client that lies about its `clientInfo` to get the unfiltered
listing gains nothing it could not already have by calling the tool it could not see. There is
no boundary here to put a hole in.

So the word everywhere — in the UI, in the changelog, in `admin.md` — is **not advertised**,
never "blocked". A deployment that needs a tool a model *cannot invoke* wants a different
feature: a per-backend allowlist enforced in `router.py` and written to `servers.yaml`, with
the refusal semantics of a dead backend. This is deliberately not it.

## It holds what is hidden, not what is allowed

"A backend nobody has touched publishes everything" is then the **empty** state rather than a
sentinel. An allow-list needs a tri-state — `None` meaning all, `set()` meaning none, a set
meaning these — and `None` against `set()` is exactly the distinction somebody eventually gets
backwards, in the direction that publishes nothing.

The stronger argument is about a tool that did not exist yet. A backend emits `list_changed`
and grows a tool; with a hidden set it is published, and an operator who did not want it
unticks it. With an allow-list written before it existed it is absent, silently, from a system
whose entire purpose is telling a model what exists. One of those failures has a symptom.

## Nothing is pruned against the live listing

A name for a tool the backend no longer publishes stays in the set. It is a membership test
that never matches, and if the tool comes back it comes back hidden — which is the last
intention anybody expressed about it.

Pruning would have to run against a listing, and a listing is a fan-out that a down backend
answers with nothing. The sweep that tidied up would be the sweep that quietly un-hid
everything a backend was not running to publish.

**The one exception is a backend a reload removed**, where `drop_backend` runs in the same
loop as [`notifications.md`](notifications.md)'s `subscriptions.drop_backend` and for the same
reason: there is nothing left for the entry to name. A restart, an Enable/Disable and an Edit
all keep the selection, because those are the same backend.

## In memory, and gone on restart

Nothing is written to `servers.yaml`. The selection is a property of the work somebody is
doing this afternoon, not of the deployment, and persisting it would put a context knob into a
committed file that [`config_writer.md`](config_writer.md) round-trips and a second operator
reviews in a diff.

The cost is stated rather than hidden: a daemon that restarts advertises everything again.
That is the right default for a thing whose failure mode is a tool the model cannot see, and
it is why the admin UI shows the hidden ones rather than dropping them from its own list.

## The bench is exempt, and that is a coupling worth naming

`exempt` is true for a session whose `clientInfo.name` is `BENCH_CLIENT`, the literal
`src/mcp_gateway_ui/rpc.js` already sends. The admin UI's columns speak MCP
([`webui.md`](webui.md)), so the checkbox list is built from the same `tools/list` everything
else in the page uses — **and without the exemption it could only ever offer the tools that
are already visible, which would make un-hiding impossible.**

Keyed on `clientInfo` rather than a `/mcp?bench=1` query param because the page sends
`initialize` identically in a browser and inside the Tauri window: no Rust change, no
`desktop.yml` run across three runners, and it covers Streamable HTTP as well as the
WebSocket, where a path-level flag would have to be threaded through twice. A query param
would also be *more* forgeable, not less, so it buys no honesty for the cost.

## The filter is applied in `gateway.list_tools`, not here and not in the catalogue

This module is a set and some predicates; it never sees a listing.

[`catalogue.md`](catalogue.md) is where it does not go. `Catalogue.tools()` records
`skipped_tools` and `unresolved_ui_templates` as it walks, and `counts()` reads the raw cache —
both of which `gateway__backend_health` reports. Filtering there would make health say a
backend publishes less than it does, which is a different claim and a false one. The cache
stays the backend's own answer; what is advertised is a property of who is asking, so it is
decided where the asker is known.

## Whole-set replace

`set_hidden` takes the complete set for one backend rather than toggling a name. Select-all
and deselect-all are one call each, the payload is idempotent, and two `/admin` connections
cannot interleave into a half state. Two operators racing is last-writer-wins, which is the
trade [`admin_channel.md`](admin_channel.md)'s `admin.backend.update` already makes.

It returns whether anything changed, which is what the caller guards the
`notifications/tools/list_changed` broadcast on — an identical set re-sent is silent.
