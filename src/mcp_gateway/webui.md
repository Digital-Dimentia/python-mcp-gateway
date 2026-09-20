# `webui.py` — the admin UI, and why it is served from here
## Two hosts, one directory

These files are served over HTTP by this module *and* loaded off disk by the desktop shell
(`../desktop/README.md`). There is deliberately one copy: `../desktop/.staging/ui` is a view of
this directory rebuilt by `scripts/stage_ui.py`, and the copy it makes takes `ASSETS` as its
manifest, so the app can never ship a file this module would not serve.

One file exists only for the second host. [`tauri-transport.js`](../mcp_gateway_ui/tauri-transport.js) swaps
`rpc.js`'s transport for one that talks to sockets the Tauri host already opened — with a
real `Authorization: Bearer` header, which a page cannot send. `index.html` loads it
unconditionally and in a browser it finds no Tauri and returns, so this module serves a file
that will never be used. That costs one small GET, and the alternative is a second copy of
the whole directory that drifts.

The transport seam in `rpc.js` is worth its own sentence, because it is not only for the
shell: opening a socket is one replaceable function and everything else — reconnect, the
backoff, the pending table, the MCP handshake — is not. `tests/ui/` drives those through
scripted frames because of it, which was impossible when the only way in was a stub written
over `globalThis.WebSocket`.

The Tauri window's content security policy is *narrower* than the one below: its
`connect-src` names `ipc:` and drops `ws:`/`wss:` entirely, which is the machine-checkable
form of "that page opens no sockets". `tests/test_desktop_layout.py` asserts it.


The UI is, in full: [`index.html`](../mcp_gateway_ui/index.html), [`style.css`](../mcp_gateway_ui/style.css),
[`app.js`](../mcp_gateway_ui/app.js), [`rpc.js`](../mcp_gateway_ui/rpc.js), [`schema_form.js`](../mcp_gateway_ui/schema_form.js),
[`render.js`](../mcp_gateway_ui/render.js), [`clipboard.js`](../mcp_gateway_ui/clipboard.js),
[`markdown.js`](../mcp_gateway_ui/markdown.js), [`format.js`](../mcp_gateway_ui/format.js),
[`screens/basics/variables.js`](../mcp_gateway_ui/screens/basics/variables.js), [`screens/basics/detail.js`](../mcp_gateway_ui/screens/basics/detail.js),
[`screens/basics/primitives.js`](../mcp_gateway_ui/screens/basics/primitives.js), [`screens/basics/results.js`](../mcp_gateway_ui/screens/basics/results.js),
[`panel_declarative.js`](../mcp_gateway_ui/panel_declarative.js), [`screens/basics/panel.js`](../mcp_gateway_ui/screens/basics/panel.js),
[`naming.js`](../mcp_gateway_ui/naming.js), [`screens/about/screen.js`](../mcp_gateway_ui/screens/about/screen.js),
[`screens/about/tour.js`](../mcp_gateway_ui/screens/about/tour.js), [`screens/about/slides.js`](../mcp_gateway_ui/screens/about/slides.js),
[`screens/connection/screen.js`](../mcp_gateway_ui/screens/connection/screen.js),
[`screens/basics/screen.js`](../mcp_gateway_ui/screens/basics/screen.js),
[`theme.js`](../mcp_gateway_ui/theme.js),
[`tauri-transport.js`](../mcp_gateway_ui/tauri-transport.js) and [`logo.svg`](../mcp_gateway_ui/logo.svg). No build step,
no bundler, no dependency — ES modules the browser loads directly. This module answers a GET
for one of them.

`logo.svg` is the odd one and is here deliberately: it is the stock mark, worn by the page
header and the favicon, and a deployment's shortest rebrand is replacing that one file. A
*configured* `branding.icon` is not served from here at all — it names a file outside this
package, and it reaches the page inline over `/admin` instead. See
[`branding.md`](branding.md).

## The frame

A header with the name, the screen selector, the configured servers, and the theme toggle;
a content panel; and a
footer. The footer is
where the socket pills, the gateway meta line and the global actions live: they are status
and escape hatches, not the work, and the work is the columns.

**The bars are the frame, and the content panel is the only part a screen replaces.** The
header and the footer answer what the *gateway* is — which servers are configured, where it
is listening, what it read, what it has been logging — and that is true whatever you are
looking at, so a screen that redrew them would be redrawing the same answer in its own hand.
Between them, exactly one `<section data-screen>` is displayed.

There are two. **Basics** is the work surface — the three columns described below — and the
screen any page opens on; its `<section>` keeps the class `columns`, because that class names
the *layout* the stylesheet is written around and `data-screen` names the screen.
**About** is a page *about* the gateway rather than a surface for driving it: the endpoint
to point a client at, the two files it read, and a row per configured server — indicator,
name, description, state, the command behind it, the secrets `gateway.env` does not have, and
whatever the process said on the way down. Every one of those facts is already reachable from
Basics, in the footer or behind a server's menu or a hover; what About adds is
that they are all in one place and can be read in one pass, which is what you want in the
minute *before* you start working. It asks the daemon nothing of its own — it is the
`admin.status` and `admin.backends` the bars have already fetched — and it is redrawn only
while it is the screen on the glass.

Adding a screen is five things and no framework: an `<option>` in the header's selector, a
`<section data-screen>` in the content panel, one pair of rules in `style.css`, a module that
renders it, and its name in `ASSETS`. The `<option>` list is the register — `app.js` reads the
screens off the control rather than carrying a list of its own, because the control, the
sections and the stylesheet are already three places a screen has to be named and a fourth
would be the one that drifts.

**The screen contract.** A screen module default-exports one object and `app.js` keeps a
registry keyed by its `id` — so dispatch is a lookup, not a branch, and a new screen is an
edit to the registry and to no other line of existing code. `id` is the `<option>` value, the
section's `data-screen` and the name `<html data-screen>` wears; `refresh(state)` redraws from
the gateway's state and is called on being shown and again whenever the payloads move while
the screen is the one on the glass; `show()` is optional and is for anything that needs the
screen to actually be laid out. Basics is the only user of `show()`, and it is the reason the
hook exists: a column cannot be measured while its screen is `display: none`, so the
separators' announced values can only be recomputed once it is back.

`refresh` is *handed* the state rather than importing it. `app.js` imports the screen, so
importing `state` back out of `app.js` would be a cycle — and a screen that reads the frame's
variables across a file boundary is not a screen you can move, test or delete on its own.

[`screens/about/screen.js`](../mcp_gateway_ui/screens/about/screen.js) is About and the place the contract is
written out; beside it, [`screens/about/tour.js`](../mcp_gateway_ui/screens/about/tour.js) is the slide deck at the
top of that screen — slide n of m, and the way to n+1 — and
[`screens/about/slides.js`](../mcp_gateway_ui/screens/about/slides.js) is everything it says about this project,
which is the half that goes stale and so has a file to itself.
[`screens/connection/screen.js`](../mcp_gateway_ui/screens/connection/screen.js) is the odd one: it is served to a
browser like every other screen, and it is the only screen a browser never shows. Its
subject is the desktop host — the child process it starts, or the SSH forward it opens to a
gateway on another machine — and a browser has neither, so `app.js` removes its `<option>`
when there is no shell behind the page. One copy of the UI, two hosts, and the same rule
`tauri-transport.js` follows. Basics is coming out of `app.js` a piece at a time, biggest first:
[`screens/basics/screen.js`](../mcp_gateway_ui/screens/basics/screen.js) is the other, and it is the screen rather than the
work: the three-column layout, the edges you drag between them, and `show`. What is *in* the
columns is a module each — [`screens/basics/primitives.js`](../mcp_gateway_ui/screens/basics/primitives.js) with the hover tooltip that
belongs to its rows, [`screens/basics/results.js`](../mcp_gateway_ui/screens/basics/results.js), [`screens/basics/variables.js`](../mcp_gateway_ui/screens/basics/variables.js),
and [`screens/basics/detail.js`](../mcp_gateway_ui/screens/basics/detail.js) for the form a row opens into.

**The directory.** `screens/` holds one file per screen, and `screens/basics/` the four
columns that exist only to serve that screen — which is what the second level is saying, and
the only thing it is saying. Everything a second screen would also want stays at the top:
`naming.js`, `format.js`, `render.js`, `schema_form.js`, `clipboard.js`, `markdown.js`, and
the three infrastructure files. The nesting costs four pieces of plumbing, each written down
where it lives: `ASSETS` keys carry a `/`, `read_asset` walks a name's segments because a
`Traversable` is not a `Path`, `pyproject.toml` names each level in `package-data` rather
than sweeping with `**`, and both the allowlist check and the desktop staging compare a walk
instead of a listing.

What the `/` does **not** change is the security argument, and that is worth saying because
"there is no path join in this module" was a sentence it used to be able to write. A name is
still matched against `ASSETS` by equality, whole; the segments `read_asset` walks come from
a key of that dict and never from a request. `/ui/screens/../webui.py` does not equal a key,
so it is a 404 before any path exists — pinned in `tests/test_webui.py`, against a client
told not to normalise the path first, since curl collapses `..` itself and would otherwise
make that test pass for the wrong reason.

**What each piece is handed, and what that buys.** Every one of them takes a port from
`app.js` or takes nothing at all, and none of them imports the frame — `tests/test_webui.py`
checks the last part, because nothing inside a file can show it. The ports are small and they
are different from each other, which is the point: `screens/basics/primitives.js` gets the state, two things
it may do to the panel, and a `listingsChanged` callback, so the column that re-read a listing
never learns that vocabularies exist. `screens/basics/detail.js` gets the state, `selectionChanged` and
`pushCard`. `screens/basics/variables.js` gets the state, `ownListings`, and the panel's own `formPort`.
`screens/basics/panel.js` gets the state and `pushCard` -- and pointedly not the admin socket, because a
backend's panel has no business reaching an admin method and the shortest way to keep it that
way is to never hand it one.
`screens/basics/results.js` and `screens/basics/screen.js` get nothing, and say so rather than growing an `install`
to look like the others.

That is what makes the wiring readable in one place. The whole of `app.js`'s involvement in
the Basics screen is four calls at the foot of `Go`, and the middle one is the interesting
one: the values column's port is *composed* out of this file's `ownListings` and the panel's
form, so neither module can reach the other except through an object `app.js` wrote.

[`naming.js`](../mcp_gateway_ui/naming.js) came out of that third cut rather than being planned: `naming.py`'s
mirror — which server is this, what did the backend call it, what is a backend URI in the
gateway's address space — was in the primitives section, and by then three modules wanted it.
It is pure functions over one entry, so unlike everything else here it is *imported* rather
than handed over: there is nothing in it to inject and nothing it could reach back into.
`gatewayUri` takes the server as an argument for exactly that reason, where the copy in
`app.js` read `state.selected` off the page.

`screens/basics/detail.js` is where the two seams meet, and the shape is worth reading once. It is *handed*
the frame it needs — the state, `label` and `idOf` off `naming.py`'s mirror, `selectionChanged`
so the list can mark what is open, and `pushCard` — and it *hands out* `formPort`, the eight
functions the variables column writes through. Those live with the form rather than in the
frame because they **are** the form: a frame that knew how to put a value in a field would be
a frame that owned the panel. So `app.js` composes the column's port out of its own catalogue
helpers and that object, which is the whole of its involvement in either:

```js
variables.install({ state, ownListings, gatewayUri, ...detail.formPort });
```

The fan-out is the one thing that crosses both ways. A picked set lives in the column;
sending the form once per value happens in the panel, which asks the column what the
combinations are, writes each into the controls as an `input` event, and then collects and
sends the form exactly as a click would — so a fanned-out call is byte-identical to the one
you would have made by typing the value in yourself.

**What the variables column is handed, and why it is a longer list.** About is a renderer of
what it is given, so its whole interface is `refresh(state)`. The variables column *writes
into a form* — that is the point of it — so a one-way seam has to name every way it may do
so. `app.js` installs a port: the state and two things about the catalogue (`ownListings`,
which replaces `ownerOf` and `localName` together so `naming.py`'s mirror stays in one file,
and `gatewayUri` for the cascade), then eight form methods — `controlFor`, `putValue`,
`fillField`, `holdsOneValue`, `markFanned`, `release`, `fieldName`, `updateSendLabel`. The
column never touches the detail panel's DOM itself, and `tests/test_webui.py` asserts the
import only goes one way, because nothing inside either file can show that it does.

`release` is the one that names a rule rather than an action. A pick lives as long as its
group is on screen; change the continent and the country picked under the old one stops
existing, while the form is still holding it. So the value has to go where the pick went —
but a value *you* typed over it is yours, and the form is asked to release what it was given
and nothing else. That used to be `buryField` reaching into a control directly; as a port
method it is the same code with the rule written on it. [`format.js`](../mcp_gateway_ui/format.js) exists because of that split — uptime is on the
footer *and* on About, and a duration that reads `2h 14m` in one place and `8040s` in the
other is a difference a person notices and cannot explain. The 12-line `el` DOM shim is still
copied into each module that builds nodes, which is the older house rule and a deliberate
contrast: a DOM constructor has no decisions in it to drift, and a formatter does.

Which screen is showing is `data-screen` on `<html>`, written by `theme.js` before the first
paint for the same reason the theme and the column split are (below): restored from a
deferred module, a page parked on About would paint the columns first and then swap on every
reload. That file deliberately knows no screen names — it stores a name if it *looks* like
one, and `app.js`, which can see the options because the body is parsed by the time a module
runs, is what rejects a name that is no longer a screen. CSS cannot ask whether one attribute
equals another, so each alternate screen names itself in the stylesheet: hidden by default,
shown when `<html>` wears its name, hiding Basics when it does. Basics is therefore
what *any* unrecognised value shows — including a name left in storage by a build that had a
screen this one does not — where a rule that showed only an exact match would answer that with
a blank page. The name is written to storage by the gesture and not by the restore, so a page
that has never been switched stores nothing and pins no default.

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

**The columns are where you put them.** Each edge between two columns is a `separator` you
can drag, and the split is remembered — in `localStorage`, so it survives a reload and a
gateway restart, and is per browser rather than per daemon. The default is 28 / 34 / 38: the
variables rail is the widest of the three, where it used to be a `19rem` cap that handed
every pixel of a wide screen to the other two.

The widths are three `fr` numbers on `:root`. `fr` because it is a ratio: a dragged layout
keeps its shape when the window changes size, with no resize handler to write. The drag
measures pixels and writes the measurements straight back as `fr`, which is the same thing
said twice — a set of widths *is* a set of ratios — and is the only sound basis for the
arithmetic, because once a column is sitting on its `--col-min` floor the grid takes that
track out of the flex distribution and the rendered widths stop following the stored numbers
altogether. Dragging moves only the pair an edge divides, so the far column holds still while
you aim; the delta is clamped rather than the result, so a divider dragged past the floor and
back picks the cursor up exactly where it left it. Double-click or Home puts the default
back, and forgets the stored split rather than leaving it to return on the next reload.

The validation in `theme.js` is strict — three finite positive numbers, or the stored value
is dropped — because it is the only guard there is. A custom property is an untyped token
until it is substituted, and a token that is not a `<flex>` makes `grid-template-columns`
invalid at computed-value time, which falls back to `none` rather than to the previous
declaration: the page becomes five stacked rows. `@property` is the usual way to type a
custom property out of that failure mode, and it is not available here — the registered
syntaxes are a closed list with no `<flex>` on it, so `syntax: "<flex>"` is an invalid rule
dropped in silence, which is worse than no rule. Hence the check at the two places that
write.

`theme.js` is the one file that is not a module, and that is the whole reason it exists.
Modules are deferred, so a theme applied from `app.js` would land after the first paint —
someone who picked light on a dark machine would watch the page flash dark on every
reload. The usual fix is an inline snippet in the `<head>`, which the `script-src 'self'`
policy below rules out, so it ships as a blocking file instead. It writes `data-theme` on
`<html>`; with no attribute the page follows `prefers-color-scheme`, which is what it did
before there was a toggle.

The column split is restored there too, for the same reason and with a louder symptom: from
a deferred module, anyone who had dragged their columns would watch them jump the width of
the screen on every reload. `theme.js` only ever *overrides* the split, so the defaults exist
once, in the stylesheet, and resetting is removing the override. `app.js` owns the gesture
and nothing else.

## The servers in the header

Each configured server is a pill in the header bar, and the pill carries only two things:
the status indicator and the name. A row of servers is a row you read sideways, and a
description on every button makes that unreadable at four of them — so the description moved
into the menu, where it has room to wrap. The menu is the rest of what the left column's
rows used to hold: the description, the error the backend failed with, the credentials
missing from `gateway.env`, and the four controls — Restart, Enable/Disable, Edit, Remove.

**The pill is two buttons.** The name selects the server for the primitives column; the
caret beside it drops the menu, and neither does the other's job. They were one button that
did both, which meant you could not look at a server's primitives without a menu landing
over the columns you were about to read, and could not open the menu of the server you were
already on without it toggling. They are still drawn as one pill, divided by a hairline
rather than a gap: two gestures, one server. A caret appears only where the menu has
something in it. A click anywhere else, or Escape, puts the menu away. The row scrolls sideways rather than wrapping, because the header is one bar
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
| Results | `/mcp` | What came back, rendered, with the raw payload one click away — and the Clipboard, which writes the lot out for an agent |
| Variables | `/mcp` | The values that server publishes for its own parameters, each one a button that fills the open form |

**All three columns speak MCP, not `admin.*`.** That is the design decision the rest
follows from. A UI that asked `/admin` for a tool listing would be showing its own
rendering of the catalogue, and the whole value of a test bench is that what you exercise
in it is byte-identical to what the model gets — same `tools/list`, same namespacing, same
`tools/call`, same `isError` semantics. See [`session.md`](session.md) for that method table
and [`catalogue.md`](catalogue.md) for what it publishes. `admin.*` now answers only the
header and the footer.

## The Clipboard

The Results column's second button turns the session into one text document — every call
with its request and its answer, then every value the right-hand column is offering and
which of them are picked — for pasting into an agent.

**The page does not write that document.** It posts a *snapshot* of its two right-hand
columns with `admin.clipboard.put`, and the daemon renders it; the modal shows what came
back, in a plain textarea, and the Copy button copies whatever is in the box. The reason is
[`gateway__clipboard`](clipboard.md): the same text is a tool call on `/mcp`, so a model can
read the bench without anyone pasting anything, and two renderers of one document would
drift. [`clipboard.js`](../mcp_gateway_ui/clipboard.js) is the modal and the publisher;
[`clipboard.md`](clipboard.md) is the document.

The snapshot is published on a debounce whenever either column changes, not only when the
modal opens — otherwise the tool would report whatever the bench looked like the last time
somebody happened to press a button, with nothing on screen to explain the staleness.

The detail panel's send button is pinned to the foot of the panel rather than parked after
the form. The panel scrolls, and a form one field taller than it would otherwise push the
one button that does anything out of sight — which reads, exactly, as a form with no way to
send it.

## The variables column

A server that publishes `zoo://animals/{id}` usually publishes `zoo://animals` beside it: a
listing small enough to send whole, and a template for the per-member read that would not
be. That pair is a **vocabulary** — the set of values `id` may take — and this column is
those vocabularies, one group per variable, every value something you can spend.

What follows is why the column behaves as it does. The same contract written outward, for
somebody implementing a server against it, is in
[GET_STARTED.md](../../GET_STARTED.md#injectable-values-in-your-own-server).

**It starts as a menu, not as a column.** A select names every vocabulary the selected server
publishes; choosing one opens it, and whatever cascades from it, beneath. Nothing is read
until then. Two reasons, and the first is the one that matters: `resources/read` is a call to
a live backend, so opening every vocabulary on sight would spend a read on each one before
knowing whether anybody wanted it. The second is what it looks like — a column that arrives
full of groups, some of them empty until a pick elsewhere fills them, reads as clutter rather
than as an offer.

A `×` on each opened group puts it back in the menu, taking the chain under it and every pick
in either. The **bodies are kept**: those are a cache of what a live backend said, and
re-opening a vocabulary should not cost a second read. The picks are not — a pick is a claim
about what you meant, and a claim in a group that is no longer on screen is one nothing can
show you or take back.

**Pairing is read off the URIs, not guessed.** A template's fixed prefix, up to its first
`{`, is the listing's URI: `zoo://animals/{id}` names `zoo://animals`, and `zoo://echo/{word}`
names nothing, so it contributes no group. The comparison is on the backend's own spelling,
after the gateway's `mcpgw://<server>/<percent-encoded>` wrapper is unwound.

**One listing is one group.** Two templates can share a fixed prefix —
`zoo://continents/{continent}/countries` and the longer one beneath it both cut back to
`zoo://continents` — and both the body cache and the picks are keyed by the listing's URI, so
a second group on that key would alias the first one's values and its picks. The simplest
pairing wins: fewest variables, then shortest, then alphabetical, so the answer does not
depend on the order the templates were listed in. Nothing is lost by dropping the others; a
template taking a value this listing does not publish is not this listing's template, and is
reached through the body of the one that is.

That rule is also what keeps this column from reading a server's resources at large.
`resources/read` is a call to a live backend — `zoo://ticks` in the fixture exists precisely
to prove a read can move state — so **a resource that pairs with no template is never
fetched.** What is fetched is cached until the listings change or Reread is pressed.

**Values are read from JSON only.** A vocabulary has to be machine-readable to be one, and
splitting prose on newlines would turn every text resource into a list of garbage. Three
shapes are understood:

| Body | Values | Labels | May also name |
|---|---|---|---|
| A JSON Schema enum fragment | `enum` | `enumNames` | `readOne`, `narrows` |
| An array of scalars, or of records | the scalar, or `id`/`value`/`uri`/`name` | `title`/`name` | |
| An object keyed by identifier | the keys | each record's `name`/`title` | |

The first is what `zoo://animals` publishes, and the only one that carries labels *and*
names its own template: `readOne` is not a JSON Schema keyword, it is the listing saying
where one of its values is spent. When it is present it wins over the pairing found by
prefix — the server knows where its values go better than the URIs do.

## A vocabulary that narrows another

Some parameters are not independent. Which countries there are depends on the continent, so
there is no single `zoo://countries` to publish — and a listing that took a parameter would
be a *template*, which the pairing rule above finds nothing to pair.

So a listing names its own child, in the body, beside `readOne`:

| key | one of my values buys | example |
|---|---|---|
| `readOne` | a member | `zoo://animals/{id}` |
| `narrows` | another listing | `zoo://continents/{continent}/countries` |

That keeps the column's one invariant intact all the way down. A root is read because a
template's fixed prefix named it; every deeper listing is read at a URI **the server itself
produced**, expanded from the parent's `narrows` with everything the chain has bound. The
column still never goes looking through a live backend's resource space for something that
might be a vocabulary. The gateway spelling for that URI is minted rather than looked up —
`resources/read` resolves a URI rather than checking it against a listing, exactly as it does
for a template the client expanded itself.

**A group's variable comes from the body, not from the pairing.**
`zoo://continents/africa/countries` is spent on a template naming `continent` *and*
`country`, and the first of those is already decided — it is in the URI that was read — so
that group is the `country` one. The variable is settled while resolving rather than while
drawing, because the *next* link expands against it: a group that still thought it was
`continent` would write the country into the continent's segment and read a URI nobody
published.

**A narrowing group picks one.** No `one | many` switch on it. `many` means "send the open
form once per value" and reading a listing sends no form; merging two continents' countries
would invent a vocabulary the server never published, with nothing on the chip to say which
continent each country came from. Forcing `one` removes the ambiguous state rather than
papering over it. The leaf keeps `one | many`, which is where the fan-out belongs anyway.

**A group waiting on a pick is drawn, not hidden.** Dimmed, with a line saying which pick
above fills it — because being able to see that the cascade is there is the difference
between a column with one group in it and a column that has more to give. A pending group is
never read and is never given a pick of its own.

**Changing a pick buries what it decided.** The child's cache key is the *expanded* URI, so
switching Africa to Asia is a miss that fetches and switching back is a hit; and the
countries picked under Africa are deleted rather than left in `picks`, where they would go on
multiplying the send button's `×n` with nothing on screen to explain the number.

One rule covers that and the `×` both: **a pick lives exactly as long as its group is on
screen.** Every resolve drops the picks whose group is not, which is the same fact whether
the group went because its continent changed or because the whole vocabulary was closed.

**And the form gives the value back.** A buried pick is taken out of the field it had
filled, because the alternative is a template still expanding to
`…/africa/countries/nepal/animals` after you changed the continent — a form describing a
read that will miss, which is worse than an empty field. Only what that pick put there: a
value you typed over it is yours, and a pick dying elsewhere in the column is no reason to
take it away.

**A URI the server never handed over is a miss, not an empty list.** The zoo answers
`-32002` for a country under a continent it is not in — which is exactly what a client
picking from two listings out of step would send — because an empty listing would read as a
country with no animals.

**A template is how a vocabulary is found, not where it can be spent.** `zoo://animals`
pairs with `zoo://animals/{id}`, and that pairing is what makes it a vocabulary at all — but
once it is one, its values fill any field named `id`. The zoo's `zoo-prompt-animal` names
its argument `id` for exactly that reason, so the same six picks that read six resources
will expand six briefs.

**And opening a form fills it from the picks.** The two directions are the same feature seen
from either end, and a cascade needs both: you cannot pick a country before its continent, so
by the time the template that takes them is open, every pick is already made and clicking
them again is exactly what nobody should have to do. Binding is strict here — a value goes
into a field that carries its name or it goes nowhere — so opening a form can never scatter
picks across whatever fields it happened to have.

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

## Suggestions in the box itself

`completion/complete` is the protocol's own answer to the question the variables column
answers by hand: what may go in this field? Both are here, because they are for different
moments. The column is how a **person browses** a vocabulary — every value visible, several
pickable, the fan-out counted on the send button. A suggestion is how a field gets filled
**while you are typing in it**, which is what a model does and what a person does more often.

It attaches to a template's variables and a prompt's arguments, and to nothing else: MCP
defines a ref for a prompt and for a resource template, and none for a tool argument. A tool
form therefore gets no suggestions, which looks like an oversight and is not one.

**A `<datalist>`, not a `<select>`.** A select constrains, and this column already refuses to
write a value that is not among a select's options — so a constraining control here would
break the variables column's own fill. It would also be unable to hold the `[a, b]` a
fan-out writes. A datalist offers without constraining, which is what a suggestion is.

**The other fields of the same form are the context.** That is the cascade: `continent`,
already filled in, is sent as `context.arguments` when `country` asks, and the server
answers with that continent's countries. A field holding a fan-out contributes nothing —
several values is not *the* value that narrows anything.

**It asks on focus as well as on input**, because the useful moment is the one before
anything has been typed, and debounces so a typed word is one request rather than five.
Answers are sequenced, since a WebSocket has no `AbortController` and a slow reply must not
overwrite a newer one.

**Nothing here can fail loudly.** A gateway that does not know the method, a backend that is
down, a request that times out: each clears the list and says so in the field's tooltip. A
suggestion that broke the form it was helping with would be worse than no suggestion. The
whole affordance is gated on the `completions` capability in the `initialize` result, the
same way the prompt and resource listings are.

A pick in the variables column arrives as a real `input` event, so picking a continent there
re-narrows the `country` suggestions with no extra wiring.

## The gate has three modes, and the first one claims nothing

The overlay in front of the page is `connecting`, `blocked` or `failed`, and separating the
first from the second is the whole of python-mcp-gateway-3vk.

A daemon that is still binding refuses connections. At the level a page sees, that is
indistinguishable from a wrong access key — and it is overwhelmingly the commoner case at
load, because the page is usually opened *by* the thing that just started the daemon. The
gate used to answer the first closed socket with "this gateway may require an access key",
then connect a second later and clear it. Every launch looked like a failure that fixed
itself. In the desktop shell it was worse: the host's first `gw_open` answers "not listening
yet" while a cold interpreter imports, so the accusation was guaranteed rather than likely.

So a closed socket is evidence of `connecting` and nothing more, until the retry budget is
spent — and in the shell it never becomes anything more, because there is no key there to be
wrong. The page starts *visible* in `connecting`, rather than hidden: a blank page that
suddenly becomes an error is the same startle in a different costume.

The modes are ranked, and `showGate` refuses to let a lower rank overwrite a higher one.
Two things write to this panel — the sockets, and (in the shell) the host's supervisor
events — and only the second can know that the gateway *failed*. Without the rank, a socket
closing after the host reported a bad `servers.yaml` would paint a hopeful splash over the
one message that said what was actually wrong.

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
carrier that gets a browser connected. [`app.js`](../mcp_gateway_ui/app.js) takes the key out of the
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
which is most of the development loop for the files in this package.
`Cache-Control: no-store` says the same thing to the browser.

`importlib.resources` rather than `__file__` arithmetic, so a wheel install works the same
as a checkout — the assets ship as package data, declared in `pyproject.toml`.

## How the JavaScript is tested

Three layers, and the reason there are three is that each one is cheap where the next is
not.

1. **`node --check` on every module**, from
   [`tests/test_webui.py`](../../tests/test_webui.py). Says they are valid ES modules, and
   nothing more.
2. **`node --test` over [`tests/ui/`](../../tests/ui/)**, driven from
   [`tests/test_webui_js.py`](../../tests/test_webui_js.py). jsdom, so the real modules run
   against a real `index.html`. What is covered is the variables column's resolver — which
   listings pair, which groups a cascade produces, which picks survive a change upstream —
   and both directions of the fill path: a value picked into an open form, and a form opened
   onto picks already made.
3. **A browser**, against [`examples/zoo_server.py`](../../examples/zoo_server.py), one tool
   per JSON Schema construct. Still the only thing that says the page *renders*.

**This reverses the position this document used to take**, which was that the UI has no
Node toolchain, is not acquiring one, and that what the modules do is verified by eye. What
changed is that the variables column stopped being a renderer of listings and acquired
state — opened sets, chains, picks with lifetimes. Two logic bugs landed in one sitting,
both in code no Python test can reach: a cascade child inherited its parent's variable and
built `zoo://continents/congo/countries//animals`, and a form opened onto picks already made
came up empty. The second shipped, and a person found it in a browser. Both took about
thirty lines of harness to catch once there was a DOM to run against. A third — a pick that
could never be put in a schema-`enum` field, because `putValue` was matching values against
option *indices* — fell out of writing the suite.

The cost is paid in three deliberately small ways:

- **jsdom is dev-only**, in [`tests/ui/package.json`](../../tests/ui/package.json). Not a
  runtime dependency, not in `pyproject.toml`, not in the wheel. The daemon still ships no
  Node anything and the CSP argument below is untouched.
- **The suite skips rather than fails** when node or jsdom is absent, so a checkout that has
  never run `npm install` stays green on `make test`. `make ui-deps` is what turns the skip
  into a run. One test command, one CI toolchain.
- **The seam is narrow.** `app.js` exports its internals at the foot of the file — inert in
  the browser, since nothing imports the page's entry module — and what is exported is the
  resolver and the fill path. Rendering, styling and the sockets are not tested: their value
  is in being looked at, which is what layer 3 is for.

And the honest limit: **jsdom is not a browser.** It has no layout, so `scrollIntoView` and
`setPointerCapture` are stubs in the harness, and a test that passes there can still be
wrong in Chrome. Layer 3 does not go away.

## The Content-Security-Policy is a promise being kept

`default-src 'none'` with `script-src 'self'` and `style-src 'self'`: the page loads nothing
it did not ship. No CDN, no font host, no analytics. An admin console for a daemon that
holds every credential on the machine should work on a machine with no route to the
internet, and should not be one compromised npm package away from exfiltrating what it can
see. The header is what stops a future edit from quietly adding one.

## What is not here

This module does not serve the *data* and never touches the gateway. It answers GETs for
the files in `ASSETS` and nothing else. Everything the UI shows comes over `/admin`
([`admin_channel.md`](admin_channel.md)) or `/mcp` ([`session.md`](session.md)), which is
why the security argument above is about files rather than about access control.
