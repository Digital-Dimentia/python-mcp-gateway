# `webui.py` — the admin UI, and why it is served from here

The UI is six files: [`index.html`](ui/index.html), [`style.css`](ui/style.css),
[`app.js`](ui/app.js), [`rpc.js`](ui/rpc.js), [`schema_form.js`](ui/schema_form.js) and
[`render.js`](ui/render.js). No build step, no bundler, no dependency — ES modules the
browser loads directly. This module answers a GET for one of them.

## Three columns

| Column | Source | What it is for |
|---|---|---|
| Backends | `/admin` | What is configured, what is running, what is missing a credential, and the editor that changes it |
| Primitives | `/mcp` | The selected backend's tools, prompts, resources and templates, with a form built from each one's own schema |
| Results | `/mcp` | What came back, rendered, with the raw payload one click away |

**The middle and right columns speak MCP, not `admin.*`.** That is the design decision the
rest follows from. A UI that asked `/admin` for a tool listing would be showing its own
rendering of the catalogue, and the whole value of a test bench is that what you exercise
in it is byte-identical to what the model gets — same `tools/list`, same namespacing, same
`tools/call`, same `isError` semantics. See [`session.md`](session.md) for that method table
and [`catalogue.md`](catalogue.md) for what it publishes.

## Why the same port

Same origin. The page opens `ws://…/mcp` and `ws://…/admin`, and
[`transport_ws.py`](transport_ws.md) now refuses a socket whose `Origin` names anywhere
else. Serving the page from a second port or a separate static server would mean either
widening that check or explaining to every user why the browser can see the page but not
the gateway.

## Why the assets carry no access key

The two sockets require the key. These files do not.

A browser cannot put an `Authorization` header on a navigation. The only way to gate a page
is a query parameter, which is precisely how a key ends up in shell history, in a bookmark,
in the referrer of every outbound link, and in any proxy log between here and there —
the carrier `transport_ws.py` already documents as wrong on principle and accepts anyway
because nothing else works.

What would that buy? The assets are inert. They hold no backend list, no configuration and
no credential; everything a person can actually *see* arrives over a socket that did check
the key. Gating the shell would trade a real secret-leak channel for no protection at all.

The banner prints the URL with `?key=` when a key is configured, because that is the one
carrier that gets a browser connected. [`app.js`](ui/app.js) takes the key out of the
address bar with `history.replaceState` the moment it reads it, and keeps it in
`localStorage` instead.

## An allowlist, not a path join

`asset_for` resolves a request path against `ASSETS`, a fixed tuple of filenames. There is
no `Path(root) / requested` anywhere in this module, so there is no traversal to get wrong —
no `..`, no encoded separator, no symlink, no case-insensitive-filesystem surprise, and
nothing to review each time someone touches it.

The cost is that adding a file to `ui/` means adding its name here.
[`tests/test_webui.py`](../../tests/test_webui.py) asserts the tuple and the directory agree
exactly, so the failure mode is a red test rather than a 404 nobody can explain.

## Assets are read on every request

Caching them would save microseconds and cost the ability to edit `app.js` and press reload,
which is the entire development loop for the six files in this package that no Python test
can cover. `Cache-Control: no-store` says the same thing to the browser.

`importlib.resources` rather than `__file__` arithmetic, so a wheel install works the same
as a checkout — the assets ship as package data, declared in `pyproject.toml`.

## The Content-Security-Policy is a promise being kept

`default-src 'none'` with `script-src 'self'` and `style-src 'self'`: the page loads nothing
it did not ship. No CDN, no font host, no analytics. An admin console for a daemon that
holds every credential on the machine should work on a machine with no route to the
internet, and should not be one compromised npm package away from exfiltrating what it can
see. The header is what stops a future edit from quietly adding one.

## What is not here

This module does not serve the *data* and never touches the gateway. It answers GETs for
six files. Everything the UI shows comes over `/admin`
([`admin_channel.md`](admin_channel.md)) or `/mcp` ([`session.md`](session.md)), which is
why the security argument above is about files rather than about access control.
