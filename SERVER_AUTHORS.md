# Writing an MCP server that sits behind this gateway

**You do not have to do anything.** A conforming MCP server works behind this gateway
unchanged: no extension to negotiate, no gateway-specific method, no dependency on this
project. If that is all you needed to know, stop here.

What follows is the other half — the handful of things a server can publish that this
gateway will *use*, and the handful of things it does to your server whether you publish
them or not. Every one of them is ordinary MCP. None of them is required.

- [What the gateway does to your server](#what-the-gateway-does-to-your-server)
- [Injectable values: the one feature worth publishing for](#injectable-values-the-one-feature-worth-publishing-for)
- [A user interface for your tool](#a-user-interface-for-your-tool)
- [Completions](#completions)
- [Logging and subscriptions](#logging-and-subscriptions)
- [Testing against a real gateway](#testing-against-a-real-gateway)

## What the gateway does to your server

Worth knowing because it changes what your users see, not because it asks anything of you.

**Your tools are renamed.** A tool called `search` on a backend configured as `docs` reaches
the model as `docs__search`. The separator is `__`, a server name may not contain it, and
routing is a split on the *first* occurrence — so a tool of your own that already contains
`__` needs no escaping. `github__create__issue` splits into `github` and `create__issue`.
See [`naming.md`](src/mcp_gateway/naming.md).

**Your resource URIs are rewritten.** `zoo://animals` reaches the client as
`mcpgw://zoo/zoo%3A%2F%2Fanimals`, because two backends can publish the identical URI and
the original is therefore ambiguous. The gateway unwinds this before anything reaches you:
you write ordinary URIs and think about nothing else.

**`gateway` is a reserved server name**, so the gateway's own meta-tools cannot be shadowed.

**Your list pages are walked to exhaustion.** Return `nextCursor` and the gateway follows
it; the client sees one list. There is a page ceiling, so an endless cursor is a bounded
failure rather than a hang.

**Both protocol revisions are accepted** — `2025-06-18` and `2024-11-05`. Answer
`initialize` with either.

**You may be a process or a URL.** A stdio server is spawned as a child with an environment
built from an allowlist; a server already running is reached over Streamable HTTP with
`url:` and `headers:`. Your side of both is identical. The deprecated HTTP+SSE transport
(`2024-11-05`) is *not* supported — front such a server with a Streamable HTTP proxy. See
[`mcp_http.md`](src/mcp_gateway/mcp_http.md).

**Say why you will not start.** If your server exits during startup, the gateway reports the
exit status and your last few stderr lines to the operator. A server that writes
`Error: /srv/code does not exist` to stderr before exiting has told them everything they
need; one that exits silently has not.

## Injectable values: the one feature worth publishing for

This is the only thing in this document that changes what the gateway's admin UI can do,
and it is a purely additive change to a server you already have.

A server that publishes a **template** like `zoo://animals/{id}` usually publishes a
**listing** beside it at `zoo://animals`: the set of values `id` may take, small enough to
send whole. That pair is a *vocabulary*. The admin UI turns each value into a chip that
fills the field it belongs in — and in `many` mode sends the form once per value, so six
values are six real tool calls.

**The pairing rule, in one sentence:** a template's fixed prefix, up to its first `{`, must
be the listing's URI.

| template | pairs with | why |
|---|---|---|
| `zoo://animals/{id}` | `zoo://animals` | prefix, trailing separator trimmed |
| `zoo://echo/{word}` | — nothing | no resource is published at `zoo://echo` |

This rule is also a promise to your users: **a resource that pairs with no template is never
read.** `resources/read` is a live call that may move state, and the UI will not go fishing
through your resource space looking for something that might be a set.

The checklist, so you can tell whether you are done:

- [ ] The template is in `resources/templates/list`, the listing in `resources/list`.
- [ ] The listing's URI is exactly the template's text before its first `{`, minus any
      trailing `/`, `#`, `?` or `&`.
- [ ] `resources/read` on the listing returns a **text** content whose body parses as JSON.
      Prose is refused deliberately: a vocabulary has to be machine-readable to be one.
- [ ] The body names `readOne` (one of my values buys a member) or `narrows` (one of my
      values buys another listing), so which template the values are spent on is stated
      rather than inferred. A cascade past the first level needs `narrows`.
- [ ] The template's variable has the same name as the field it should fill — `{id}` fills
      an `id` argument. Matching loosens in steps (exact, then case- and
      separator-insensitive, then a field whose name *ends* in it, like `animalId`).
- [ ] Reading the listing is cheap and side-effect-free. It is read on open, and again on
      Reread or a `list_changed`.

**The full version, with the three accepted body shapes, cascading, and worked JSON, is
[GET_STARTED.md § Injectable values, in your own server](GET_STARTED.md#injectable-values-in-your-own-server).**
Every rule the UI follows is in [`webui.md`](src/mcp_gateway/webui.md).

## A user interface for your tool

MCP Apps (SEP-1865). A tool carries `_meta.ui.resourceUri` naming a resource under `ui://`,
and the host fetches and renders it. Two content types are rendered here:

| mime type | what it is |
|---|---|
| `application/json;profile=mcp-app-declarative` | a described interface the gateway draws |
| `text/html;profile=mcp-app` | a document you wrote, run in the viewer's browser |

The gateway rewrites the `ui://` address the same way it rewrites every other resource URI,
so your panel stays reachable through the one address anybody was given. An HTML panel is
served from a single-use URL that expires in seconds, in a sandbox with no access to the
window that framed it — it gets no credential, and it names the tool it wants to call rather
than being handed one. See [`ui_apps.md`](src/mcp_gateway/ui_apps.md) and
[`panels.md`](src/mcp_gateway/panels.md).

## Completions

`completion/complete` is forwarded, including `context.arguments` — so a completion for one
argument can depend on what was already filled in another. A `-32601` is fine: completions
are optional within the capability, and the caller absorbs it.

## Logging and subscriptions

If you declare `logging`, the gateway pushes a level down with `logging/setLevel` and relays
your `notifications/message` to the clients whose own level admits them, tagged with your
server's name.

If you declare `resources.subscribe`, `resources/subscribe` is forwarded and your
`notifications/resources/updated` reaches exactly the sessions that asked, with the URI
rewritten into the client's address space. **Both are replayed for you** after a restart or
an HTTP session renewal: you do not have to remember what the gateway had set, because the
gateway does.

## Testing against a real gateway

`make run-dev` starts the daemon against [`examples/zoo_server.py`](examples/zoo_server.py),
which needs no credentials. The zoo is a ~1,900-line fixture that publishes one tool per
JSON Schema construct, the flat `zoo://animals` vocabulary, a three-level continent cascade,
completions with `context.arguments`, a prompt whose argument the same picks fill, and both
kinds of panel — with the reasoning written beside each part. It is the reference
implementation of everything in this document.

Point the gateway at your own server by adding it to `servers.yaml`
([GET_STARTED.md § Configure your own MCP servers](GET_STARTED.md#configure-your-own-mcp-servers)),
then open the admin UI and drive it there. What you exercise in that UI is byte-identical to
what a model gets.

## Where to go next

- [GET_STARTED.md](GET_STARTED.md) — running a gateway, and the long form of injectable values
- [ARCHITECTURE.md](ARCHITECTURE.md) — the module map, if you want to know why any of this is shaped as it is
- [`webui.md`](src/mcp_gateway/webui.md) — every rule the Injectable values column follows
- [`router.md`](src/mcp_gateway/router.md) — how a namespaced call finds your server
