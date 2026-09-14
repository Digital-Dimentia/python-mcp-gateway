# `branding.py`

What this gateway calls itself, and the picture it wears. One optional block in the
catalogue:

```yaml
branding:
  title: "Acme Internal Tools"     # what a person reads
  name: acme-tools                 # what an MCP client receives in serverInfo
  icon: ./brand/acme.svg           # optional; relative to servers.yaml, not to the cwd
```

The shipped `servers.yaml` carries this block already, filled in with the stock values, so
rebranding is editing two lines in a file that is already open rather than discovering that
a key exists. Delete the block and nothing changes either: the title is `MCP Gateway` and
the `serverInfo` name is `mcp-gateway`, exactly as every released version has reported.

## Two icons, and only one of them is configuration

The **stock mark** is [`ui/logo.svg`](webui.md) — a file in the package, served at
`/ui/logo.svg`, staged into the desktop shell with the rest of the assets, and named twice
in `index.html`: the favicon, and the header image. It is what the page wears on its first
paint, before any socket has answered, and what it goes back to when no `icon:` is
configured. Swapping that file is the shortest possible rebrand, and in a checkout it is the
obvious one.

A **configured** `icon:` is a different thing: a file outside the package, named by a
catalogue the daemon read at run time. It never becomes an asset — `webui.py`'s allowlist is
the security boundary and does not grow a path join for a logo — it is read and handed to
the page inline, as described below. `admin.status` reports `icon: null` when there is none,
which the page reads as "wear the stock mark" rather than "wear nothing".

The division is what makes a pip-installed deployment brandable at all: editing a file
inside `site-packages` is not a thing to ask of anyone, and `icon:` is the answer for them.

## Where it lands

| Surface | Field | How it gets there |
| --- | --- | --- |
| Browser tab, header, favicon | `title`, `icon` | the markup, then `admin.status` → `app.js` |
| Desktop window title | `title` | the same payload, then `setTitle` over IPC |
| Desktop app icon, `.app` name | `icon`, `title` | build time — `make tauri-brand` |
| `serverInfo` on `/mcp` | `name`, `title` | `gateway.server_info()` |
| `--check`, `--list`, startup log | `title`, `name` | `cli.py` |

## Why two names

`title` and `name` are separate because their readers are. `title` is prose for a human —
spaces, capitals, an ampersand if the company has one. `name` goes over the wire in
`serverInfo`, where clients put it in config files, log lines and lock keys, so it keeps the
shape of an identifier and whitespace in it is refused with a message pointing at `title`.

Folding the two into one field gives you either a window titled `acme-tools` or a client
config key called `Acme Internal Tools`. Both are somebody's bug, and the second one is a
bug in a file this program does not own.

## Why a configured icon travels as a `data:` URI

There are two hosts for one admin UI, and only one of them can fetch a URL.

The desktop shell loads the page off disk under
`img-src 'self' data:; connect-src ipc: http://ipc.localhost` — see
[`../desktop/README.md`](../desktop/README.md). An `<img src="http://127.0.0.1:…">`
is simply not loadable there, and widening that CSP would open the window to the network for
the sake of a logo. So the bytes ride inside the `/admin` payload the window is already
receiving: one path, both hosts, byte-identical rendering.

It also keeps [`webui.py`](webui.md)'s allowlist a list of files this package ships.
Serving a *configured* file would have meant the one thing that module refuses to contain —
a path join between a request and a directory — or a second unauthenticated endpoint whose
content is chosen by the config file. Neither is worth a picture. `logo.svg` is in that
allowlist precisely because it is **not** configuration: it is one more file shipped beside
`style.css`, and it is fetched like one.

The cost is real and is why there is a cap: 128 KiB before base64, checked at parse time and
again at read time. An SVG logo is a couple of kilobytes; a PNG sized for an icon rarely
clears sixty. A photograph is not an icon, and the refusal says so.

## Loud at parse time, quiet at serve time

A missing file, an extension outside the table, an oversized image, an unknown key, a
control character in the title — all `ConfigError` at load, naming the file. A branding block
that half-applies is worse than one that refuses: nobody looks at a plain header and
concludes their logo failed to load, they conclude they edited the wrong file. The same
stance [`config.py`](config.md) takes on an unknown key, and it means `make check` reports a
broken logo without binding a port.

`icon_data_uri()` is the mirror image. It runs while answering `admin.status`, so a file that
has been deleted or swollen *since* startup logs a warning and yields `None` — which the page
reads as the stock mark. A logo that goes missing under a running daemon must cost the header
its picture, never cost the page its footer, its socket pills and its reload button.

## Re-read, never cached

The bytes are read on every request, like [`webui.read_asset`](webui.md). Replacing the logo
and pressing reload is the entire edit loop for this file, and a cache would make that loop
require a restart to see. Parse time validates; serve time reads.

## What is deliberately not brandable

The tool namespace. A backend is still `github__create_issue`, and the gateway's own
meta-tools are still `gateway__list_backends`, no matter what the deployment is called.
Those names are the contract a model reasons about and other clients' configs hard-code;
renaming them per deployment would make every prompt, every log line and every
`gateway__restart_backend` call in the wild deployment-specific. White labelling is what a
person sees, not what the protocol says.
