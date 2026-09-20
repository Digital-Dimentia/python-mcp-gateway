# `panels.py` — the one endpoint that serves something a backend wrote

Everything else on this port is the gateway's own: [`webui.py`](webui.md) serves files from
this repository, `/mcp` and `/admin` serve answers this process composed. `/panel/<token>`
serves a document a **backend** wrote, to be executed by the browser that asked for it. That
is SEP-1865's HTML tier — `text/html;profile=mcp-app` — and the difference is the reason this
is a module with a doc rather than four lines in `webui.py`.

The declarative tier ([`ui_apps.md`](ui_apps.md), `panel_declarative.js`) exists precisely so
that most panels need none of this: it is JSON rendered as DOM by our own code, and nothing a
backend sends is ever parsed as markup. This module is for the panels that are markup.

## Why a URL at all

The obvious implementation hands the page the HTML as a string and frames it with `srcdoc`.
It cannot work. A `srcdoc`, `blob:` or `data:` document **inherits the embedder's policy
container**, so the panel would run under `/ui`'s `script-src 'self'` and its inline script
would not run at all. The only way to fix that from the embedder's side is to loosen the
*admin page's* policy — to let a backend's script run — which is a trade worth refusing.

A real URL gets a real response, and a real response gets its own headers.

## The lock is the response header

The URL is on this gateway's own origin, and that origin is where `/ui` keeps the access key
in `localStorage`. A document served from here that ever obtained that origin would be
reading the key.

`sandbox` on the `<iframe>` element does not settle that, because the attribute belongs to
whoever wrote the embed. Anything that opens the URL directly — a new tab, another page's
frame without the attribute — is a document on this origin. What settles it is the response:

    Content-Security-Policy: sandbox allow-scripts

which puts the document in an opaque origin however it is reached. The `<iframe>` keeps its
`sandbox` attribute anyway, on the principle that two mechanisms are better than one, but the
header is the boundary and the attribute is decoration. **Drop the header and a panel token
becomes stored XSS on the origin holding the key.** `tests/test_panels.py` asserts it
verbatim for that reason, rather than asserting "a CSP is present".

`allow-scripts` is the only token granted. Not `allow-same-origin` — that is the lock itself
— and not `allow-forms`, `allow-popups`, `allow-modals` or `allow-top-navigation`: a panel
talks to its host over `postMessage`, and a panel that could navigate the top frame could
take the admin page off the screen and put something that looks like it there instead.

## Why this may be unauthenticated, and why that is not `webui.py`'s argument

`webui.py` serves the admin UI to anyone who asks, on the grounds that those files are
*inert* — no configuration, no backend list, no credential, and nothing a person can see
without a socket that did check the key. **That argument does not transfer here.** A panel is
a backend's document and it is not inert.

This endpoint needs its own, and it has four legs plus a fifth that is not about the URL:

| | |
|---|---|
| **Unguessable** | 256 bits from `secrets.token_urlsafe` in the path |
| **Short-lived** | seconds, because the page sets an `<iframe src>` the moment it has one |
| **Single-use** | the first GET consumes it; a URL in a history file is already dead |
| **Sandboxed** | the header above, so what it serves has no origin to abuse |
| **Minted over `/admin`** | which did check the key — and revoked when that socket closes |

Two endpoints on one port are unauthenticated for entirely different reasons. Writing the
second one out here, rather than pointing at the first, is the point: the day somebody
changes `webui.py`'s reasoning, nothing silently inherits it.

## What a token is bound to

`admin.panel.open` takes the `mcpgw://` URI of a `ui://` resource, and the **gateway** reads
it, through the same [`router.py`](router.md) path a client's `resources/read` takes. The
alternative — the page posts the HTML it already fetched and the gateway serves it back — is
one round trip cheaper and gives up the two properties worth having: that the bytes at a
panel URL are a backend's own answer, and that the policy came from the `_meta.ui.csp`
[`ui_apps.py`](ui_apps.md) sanitized rather than from whatever the caller said it was.

## The policy, and a conformance gap

`default-src 'none'`, then one directive per `csp` list a backend asked for, so a host it did
not name is refused rather than defaulted in. `frame-ancestors 'self'` means the admin page
may frame a panel and a page on the internet may not — and unlike the directive below it,
that one works, because it is matched against the *embedder*, which has a real origin.

⚠️ **With `allow-same-origin` omitted the origin is opaque, so `'self'` matches nothing.**
SEP-1865's default `script-src 'self' 'unsafe-inline'` therefore collapses here to
`'unsafe-inline'` alone: a spec-conformant panel that loads a sibling `.js` works in a host
that grants it an origin and silently fails in this one. This is written down rather than
left to be discovered; a panel written for this gateway inlines its script. `'self'` stays in
the policy because it says what the directive means and costs nothing — what it does not do
is work.

## Lifetime

`PanelStore` is one per process, on the `Gateway`, for the same reason the clipboard is: the
thing that mints is an `/admin` connection and the thing that serves is a `process_request`
hook inside the WebSocket server, and neither can reach the other except through the object
they share. It is not a cache — every path out of it, a load, an expiry or a closed
connection, removes the entry.
