# `ui_apps.py`

MCP Apps (SEP-1865) is how a server ships a user interface for its tools: the tool carries
`_meta.ui.resourceUri` naming a resource under the `ui://` scheme, and a host fetches that
resource and renders it. This module is the part a **proxy** has to do, and nothing else —
no rendering, no fetching, no policy.

## Why a proxy has to do anything at all

The specification was written for a server talking to a host. Between them, this gateway
rewrites every resource URI into [`naming.py`](naming.md)'s `mcpgw://<server>/<encoded>`
address space, because two backends can publish the identical URI and the original is
therefore ambiguous.

`_meta` is not touched by any of that. [`catalogue.py`](catalogue.md) copies a tool entry and
overwrites its `name`; everything else, `_meta` included, is the backend's own bytes. So
without this module a tool reaches a client announcing `ui://zoo/panel`, the client asks for
exactly that, and the gateway answers `-32602` — the address is real on the backend and
meaningless here. The panel is unreachable through the one address anybody was given.

## The authority is never parsed

`ui://zoo/panel` puts `zoo` where a URI keeps its authority, and a gateway with a backend
named `zoo` is invited to connect the two. It must not.

That string was chosen by the backend. It has no relationship to the names this gateway gave
its backends — those come from `servers.yaml` and are validated by `validate_server_name`.
The rewrite uses **the backend that published the tool**, and treats the entire `ui://` URI
as an opaque payload:

    encode_resource_uri(publishing_backend, whole_ui_uri)

A tool published by `evil` referencing `ui://zoo/panel` therefore becomes
`mcpgw://evil/ui%3A%2F%2Fzoo%2Fpanel`. Reading it asks **evil** for `ui://zoo/panel`. `zoo`
is not consulted and cannot be.

The alternative — resolve the authority against a server name, which reads as the more
correct implementation — is a confused-deputy vulnerability. Any backend could nominate any
other backend's interface and have a host render it under that backend's identity, with that
backend's declared CSP and permissions. The design here does not mitigate that bug; there is
no code path on which it exists.

The risk that remains is social: a future contributor reading `ui://zoo/...` and "fixing" the
resolution as a convenience. `test_the_ui_authority_is_never_matched_against_a_server_name`
is there to fail when they do, and this section is here to explain why the test is not
pedantry.

## Refusing a reference costs a panel, not a tool

`_acceptable_reference` refuses a `resourceUri` that is empty, over-long, carries a control
character, or whose scheme is not `ui`. The last one matters most: `https://`, `data:` and
`javascript:` ask a host to frame content from outside the resource system altogether, which
is a hostile shape rather than a spec field we have not learned yet.

A refusal strips the reference and **keeps the tool**. Every host already knows how to render
a tool with no panel, so degrading is strictly better than dropping something a model might
have needed.

## A dangling reference is reported, never enforced

A reference to a `ui://` the backend does not publish is left in place. The check that could
be made here is not authoritative — `resources/list` is paginated and cached, and a template
may legitimately appear only under `resources/templates/list` — so a miss would be a guess.
Turning a guess into a silently missing panel is worse than a clean `-32002` at read time,
which is the same reasoning `find_tool` uses when it hands a name to a backend rather than
checking a cache first.

It is also not a security question: by the section above, a dangling reference can only ever
reach the backend that made it.

So [`catalogue.py`](catalogue.md) records it on the backend instead, beside `skipped_tools`,
and `admin.status` carries it. Keeping it advisory has a second payoff worth naming: because
the answer never changes the published bytes, invalidating a backend's *resources* does not
have to invalidate its *tools*. Enforcement would have created that edge, and a cross-cache
invalidation edge is a bug generator.

## Injection is ours to refuse; permissiveness is not ours to judge

`csp` and `permissions` do nothing in this gateway's own renderer. They matter because a
downstream host turns them into a real `Content-Security-Policy` header and real browser
grants, and the bytes reach that host through us.

CSP source lists are space-separated and directives are semicolon-separated. One entry
reading `example.com; script-src *` therefore *ends the directive it was in and starts
another*, and the host's whole policy for that frame belongs to the backend. That is header
injection, and refusing to forward it is squarely a proxy's job.

Whether `*.example.com` is too broad is a different question and not one we are entitled to
answer. The operator chose these backends; a gateway that vetoed a legal wildcard would break
working panels over a judgement it has no standing to make. Hence the line: a bare `*`
passes, a `;` does not.

`permissions` values must be real booleans for the same kind of reason, though it looks like
pedantry at a glance. A host writing `if perms.get("camera")` treats the string `"false"` as
true — a genuine grant manufactured out of a fake denial. That is a shape error, which we
catch; it is not a policy preference, which we would not.

## Unknown keys survive, and what would change that

Only the members we can *misread* are validated. Everything else under `ui` is copied
through. SEP-1865 is pre-GA — its own flat `_meta["ui/resourceUri"]` spelling is already
marked for removal — and a proxy that allowlists fields becomes the reason next year's field
does not work, in a way that is invisible from both ends.

The residual risk, stated rather than buried: a future `ui` member whose value is itself a
fragment of header syntax would pass through unexamined. That is the thing that would change
this call — a named field carrying header syntax gets its own row here, the same way `csp`
has one. An allowlist today would certainly break every future field in order to guard
against one that may never exist, which is the worse trade.

`domain` is a related limit the gateway genuinely cannot fix. If a host derives a frame origin
from it, two backends both claiming `app.example.com` collide in that host's origin space and
share storage. A URI can be namespaced; an origin cannot, and inventing a prefix would break
the backend's own expectations. The shape is validated and the limit is the host's.

## The deprecated spelling is mirrored, never invented

SEP-1865 carries a flat `_meta["ui/resourceUri"]` alongside the nested form and says it will
be removed before GA. The rule here: rewrite it if the backend sent it, never add it if the
backend did not, and when both are present let the nested one win and make the flat one
agree.

Stripping it would break a host that reads only the old spelling talking to a backend that
sends only the old spelling, which is a live pairing today. Inventing it would have the
gateway emit a field the specification is in the process of deleting. Mirroring presence is
the only rule that is both compatible now and a one-line deletion later — `FLAT_RESOURCE_URI_KEY`
is that line.

Resolving a disagreement between the two spellings *here* rather than forwarding both also
means every host downstream sees the same answer, instead of each picking for itself.

## Nothing here mutates its argument

Every function returns a new object or `None`, and `None` means "nothing changed, keep what
you have". That is not style. `Catalogue.tools()` calls this with the `_meta` of a **cached**
entry, and that cache is the backend's own answer, which health and `/admin` must go on
seeing exactly as the backend gave it. Mutating in place would also rewrite the same
reference again on the next call and double-encode it — a bug that passes every test written
against a single call.

## What this module deliberately does not do

- **It does not advertise anything upward.** `io.modelcontextprotocol/ui` is a *client*
  capability. The gateway is the server to its clients, so [`protocol.py`](protocol.md)'s
  `CAPABILITY_MANIFEST` is untouched by MCP Apps and its unconditional-by-design reasoning
  is neither bent nor challenged. There is no row to add; adding one would be the mistake.
- **It does not filter `_meta.ui` for clients that did not declare the extension.**
  `tools/list` is cached process-wide, per-client filtering would fork the catalogue per
  session, and `_meta` is MCP's designated channel for things a peer may ignore.
- **It does not fetch, cache or render.** A `ui://` resource is read through
  [`router.py`](router.md) like any other.

## Where the two tiers part company

This module is the same for both of SEP-1865's tiers, because both are addressed the same
way: a tool names a `ui://` resource, and the *resource's* `mimeType` says which tier its
document is written in. That is the whole reason the rewrite here knows nothing about
rendering.

What happens after the read is where they differ, and it is worth knowing which door a panel
went through:

| | declarative | HTML |
|---|---|---|
| `mimeType` | `application/json;profile=mcp-app-declarative` | `text/html;profile=mcp-app` |
| rendered by | `panel_declarative.js`, as DOM | the browser, in a frame |
| executes backend code | never | yes, which is what [`panels.md`](panels.md) is about |
| needs an endpoint | no | `/panel/<token>`, with its own policy |
| works in the desktop shell | yes, over the socket it already has | yes, through the shell's own `panel:` scheme |

The `csp` and `permissions` members sanitized above finally *mean* something on the second
row: [`panels.py`](panels.md) turns `csp` into the real `Content-Security-Policy` a panel is
served under. Until the HTML tier existed, that sanitising was done purely on behalf of
downstream hosts; now this gateway is one of them, which is a good reason for the rule to
have been written before it was needed.
