# `webui.py` — the admin UI, and why it is served from here

The UI is seven files: [`index.html`](ui/index.html), [`style.css`](ui/style.css),
[`app.js`](ui/app.js), [`rpc.js`](ui/rpc.js), [`schema_form.js`](ui/schema_form.js),
[`render.js`](ui/render.js) and [`theme.js`](ui/theme.js). No build step, no bundler, no
dependency — ES modules the browser loads directly. This module answers a GET for one of
them.

## The frame

A header with the name, the configured servers, and the theme toggle; three columns; and a
footer. The footer is
where the socket pills, the gateway meta line and the global actions live: they are status
and escape hatches, not the work, and the work is the columns.

The footer is three groups, and all of what they show comes from one `admin.status`.
On the left, the log tab and the two socket pills, each pill carrying the whole endpoint —
address, path, and the clients attached to that path, counted by matching `connections[].path`.
In the centre, what this gateway is: name, version, uptime. On the right, the two files,
each beside the action that re-reads it: the config names the Reload button itself
(`Reload servers.yaml`, where `Reload config` only named the act) and the env file sits
next to it under an `env` label.

All of this was once a `Gateway` `<details>` at the foot of the left column, holding the
same `admin.status` fields the footer was already showing name, version and uptime from —
so two of them were on screen twice, and the one column that has to stay readable while
you work was paying for the rest. Splitting it by what each fact answers puts every field
next to the thing it describes. Paths and addresses are the only fields long enough to
blow the bar out, so the files show their basename with the full path on the `title`.

The centre is centred on the *bar*, not on what is left over between the flanks: the
footer is a grid of `1fr auto 1fr`, so the gateway's name holds still while a client
connects, a path grows or a button appears, and the flanks clip rather than shove it.

The log is a drawer parked behind the footer, raised by the tab at the footer's left edge
and dismissed by a click anywhere outside it or by Escape. It was a `<details>` in the
column the backends used to live in, where it competed with them for the room. Its height
is measured from the content at the moment it opens and then pinned, under a `66vh` cap and
over a `10vh` floor. Three decisions, each
against an alternative that looks fine until you watch it: a fixed two thirds shows four
lines of log above a field of empty panel; continuous sizing jumps a line taller every time
the daemon logs, dragging what you were reading with it; and no floor leaves an empty
drawer the height of its own chrome.

`theme.js` is the one file that is not a module, and that is the whole reason it exists.
Modules are deferred, so a theme applied from `app.js` would land after the first paint —
someone who picked light on a dark machine would watch the page flash dark on every
reload. The usual fix is an inline snippet in the `<head>`, which the `script-src 'self'`
policy below rules out, so it ships as a blocking file instead. It writes `data-theme` on
`<html>`; with no attribute the page follows `prefers-color-scheme`, which is what it did
before there was a toggle.

## The servers in the header

Each configured server is a button in the header bar, and the button carries only two
things: the status indicator and the name. A row of servers is a row you read sideways, and
a description on every button makes that unreadable at four of them — so the description
moved into the menu the button drops, where it has room to wrap. The menu is the rest of
what the left column's rows used to hold: the description, the error the backend failed
with, the credentials missing from `gateway.env`, and the four controls — Restart,
Enable/Disable, Edit, Remove.

Clicking a button does both jobs at once: it selects the server for the primitives column and
drops its menu, so nothing needs clicking twice. A click anywhere else, or Escape, puts the
menu away. The row scrolls sideways rather than wrapping, because the header is one bar
tall and stays one bar tall — which is also why the menu is `position: fixed` and placed by
`app.js`: an absolutely positioned menu inside that scroll container would be clipped to
the bar's own height.

The `gateway` entry leading the row is synthetic. The gateway's own meta-tools have no
backend behind them, and without it they are listed by `/mcp` and reachable from nowhere in
the UI. It comes first because it is the one entry always present: anywhere else in the row
and it would move every time a server is added or removed.

## Three columns

| Column | Source | What it is for |
|---|---|---|
| Primitives | `/mcp` | The selected server's tools, prompts, resources and templates, with a form built from each one's own schema |
| Results | `/mcp` | What came back, rendered, with the raw payload one click away |
| Variables | `/mcp` | The values that server publishes for its own parameters, each one a button that fills the open form |

**All three columns speak MCP, not `admin.*`.** That is the design decision the rest
follows from. A UI that asked `/admin` for a tool listing would be showing its own
rendering of the catalogue, and the whole value of a test bench is that what you exercise
in it is byte-identical to what the model gets — same `tools/list`, same namespacing, same
`tools/call`, same `isError` semantics. See [`session.md`](session.md) for that method table
and [`catalogue.md`](catalogue.md) for what it publishes. `admin.*` now answers only the
header and the footer.

## The variables column

A server that publishes `zoo://animals/{id}` usually publishes `zoo://animals` beside it: a
listing small enough to send whole, and a template for the per-member read that would not
be. That pair is a **vocabulary** — the set of values `id` may take — and this column is
those vocabularies, one group per variable, every value something you can spend.

**Pairing is read off the URIs, not guessed.** A template's fixed prefix, up to its first
`{`, is the listing's URI: `zoo://animals/{id}` names `zoo://animals`, and `zoo://echo/{word}`
names nothing, so it contributes no group. The comparison is on the backend's own spelling,
after the gateway's `mcpgw://<server>/<percent-encoded>` wrapper is unwound.

That rule is also what keeps this column from reading a server's resources at large.
`resources/read` is a call to a live backend — `zoo://ticks` in the fixture exists precisely
to prove a read can move state — so **a resource that pairs with no template is never
fetched.** What is fetched is cached until the listings change or Reread is pressed.

**Values are read from JSON only.** A vocabulary has to be machine-readable to be one, and
splitting prose on newlines would turn every text resource into a list of garbage. Three
shapes are understood:

| Body | Values | Labels |
|---|---|---|
| A JSON Schema enum fragment | `enum` | `enumNames` |
| An array of scalars, or of records | the scalar, or `id`/`value`/`uri`/`name` | `title`/`name` |
| An object keyed by identifier | the keys | each record's `name`/`title` |

The first is what `zoo://animals` publishes, and the only one that carries labels *and*
names its own template: `readOne` is not a JSON Schema keyword, it is the listing saying
where one of its values is spent. When it is present it wins over the pairing found by
prefix — the server knows where its values go better than the URIs do.

**Clicking a value fills the open form.** Tools, prompts and templates all tag their fields
with `data-field` carrying the wire name, so one lookup covers the three. The match loosens
in steps, and stops where a wrong guess would be worse than none: the exact name, then the
same name spelt in another case or with other separators, then a field whose name ends in it
(`animalId` for `id`), then whatever you were last typing in — the only sane target for a
form that fell back to a raw JSON box. The value arrives as an `input` event rather than by
assignment, because that is what the forms recompute their preview and their problems from.
The field it landed in flashes; when nothing matched, the column says so rather than filling
something at random.

**One or many.** Each group carries a `one | many` switch, and the values under it are the
control that mode calls for: a radio in `one`, a checkbox in `many`, both inside the same
chip. Picking looks like picking either way, and the shape of the control — round or square
— is what says whether this vocabulary spends one value or several. A radio fills the field;
a set of boxes is sent once per value. Narrowing back to `one` keeps the first pick rather
than dropping the lot: that is a change of mind about the fan-out, not about the animals.

**A set shows in the field as `[axolotl, capybara]`.** Not a value the form will ever send,
and not pretending to be one: it is the fan-out, written where the fan-out will happen, so
the form shows what the send button's `×6` is counting. The field wears the accent while it
holds one. Everything that reads the form knows to ask what it means:

- the template's `Expands to` line expands once per value, a line each, and counts the rest
  past six;
- the tool's wire preview shows the **first** call, because the payload of a fan-out is *n*
  payloads and the first is the only honest single thing to show;
- the send writes the real values in one at a time, and puts the set back when it is done —
  a form left holding the last combination would read as though the picks had collapsed.

A set is only ever written into a field that can hold one. A `<select>` takes one of its own
options and a number input silently drops text it cannot parse — the browser empties the
field without saying so — so those show the value the first call will use instead.

**A fan-out is `n` ordinary calls, not a bulk method.** Nothing reaches inside a form: each
combination is written into the controls as an `input` event, and the form is then collected
and sent exactly as a click would collect and send it. So the sixth call in a fan-out is
byte-identical to the one you would have made by typing that value yourself — the same
reason the columns speak MCP rather than `admin.*`. The calls go out in sequence, so the
result cards land in the order you picked, and two vocabularies picked at once multiply:
six animals crossed with four sizes is twenty-four calls, which is why anything over eight
asks first.

Binding for a fan-out is stricter than for a click: it needs a field that actually carries
the variable's name. Filling the field you were last typing in is a helpful guess for one
value; sending a form twenty-four times into it because a box is ticked in another column is
not.

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
