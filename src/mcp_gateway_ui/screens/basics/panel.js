// The page's side of a backend-supplied panel: fetch it, draw it, and stand between it and
// the socket.
//
// `panel_declarative.js` turns a panel document into DOM. This decides *when* that happens
// and *what a panel is allowed to do afterwards*, which is the part with the sharp edges.
//
// ## A panel is a result card, not a screen
//
// Registering a screen means an `<option>` in `#screen-select`, which lives in the header,
// and the header does not change. A panel is a different `body` node handed to the
// `pushCard` the Results column already has.
//
// That is also the right shape on the merits. SEP-1865's model is "a tool result, rendered
// by an app the tool shipped", and a result card is already one call, deletable, with its
// raw JSON a click away and its place in the clipboard snapshot. The card keeps recording
// the *wire result*; the panel is a second way of looking at it, never a replacement for
// the evidence.
//
// ## Three gates before a panel reaches the socket
//
// A panel can ask for a tool call. That is the whole point of `action`, and it is the only
// thing here that could hurt.
//
// 1. **Scope.** The tool must be in the current listing *and* owned by the backend whose
//    panel this is -- a lookup, not a prefix test. `gateway__*` is refused outright: the
//    gateway's own meta-tools restart backends and reload config, and no backend's panel has
//    any business calling them.
// 2. **Consent.** The first call to a given tool from a given panel asks, naming the backend,
//    the tool and the arguments. A grant is `(card, tool)` and dies when the card does.
//    There is no global grant and nothing is remembered across a reload.
// 3. **Budget.** A ceiling per card and a minimum gap between calls, in the same spirit as
//    `FAN_OUT_ASKS_ABOVE` in `detail.js`: a speed bump, not a security boundary, because the
//    thing in front of it is a person who has to click.
//
// And the property worth stating plainly, because it is what makes the rest legible:
// **every panel-originated call goes through the same socket as a human's and lands as its
// own result card.** A panel cannot make a call you do not see. The clipboard therefore
// records panel activity with no code of its own.
//
// ## The one admin method in reach, and why
//
// A panel's calls go out on `/mcp`, and that has not changed. What did change is that the
// HTML tier needs a URL to frame, and that URL is minted by `admin.panel.open` -- so this
// module is handed a `mint` function by `app.js`, and that is the whole of its access to
// `/admin`. It takes a `mcpgw://` URI and answers with a short-lived, single-use path.
//
// It is handed in rather than imported for the same reason everything else here is: the
// route is then visible in `app.js` beside the others, and it is one function rather than a
// socket. The rule that has not moved is the one that matters -- no admin method hands back
// a credential, on this path or any other.

import { isDeclarativePanel, renderPanel } from '../../panel_declarative.js';
import { isHtmlPanel, renderHtmlPanel } from '../../panel_html.js';
import { localName, ownerOf } from '../../naming.js';
import { el, pretty, renderToolResult } from '../../render.js';

/** Calls one card's panel may make before it has to be reopened. */
export const MAX_CALLS = 20;

/** Milliseconds between one panel call and the next. */
export const MIN_GAP_MS = 250;

let port = null;

/**
 * Wire this module to the page.
 *
 * The same shape as the other three installs in `app.js`: dependencies in, nothing imported
 * from the frame. `state` carries the MCP session and the listings; `pushCard` is the
 * Results column.
 */
export function install(dependencies) {
  port = dependencies;
}

/** The `ui://` a tool names, in the gateway's address space, or null. */
export function panelFor(entry) {
  const reference = entry?._meta?.ui?.resourceUri;
  return typeof reference === 'string' && reference ? reference : null;
}

/** One card's standing permissions and spend. Dropped with the card. */
function newLedger() {
  return { granted: new Set(), calls: 0, lastAt: 0 };
}

/**
 * Ask before a panel calls a tool.
 *
 * Built here rather than in `index.html` because it is the panel's, and a dialog in the
 * markup that only one module ever opens is a thing to keep in step for no reason. Returns
 * `'once' | 'always' | 'deny'`.
 */
function askConsent({ backend, tool, args }) {
  const dialog = el('dialog', { class: 'panel-consent' });
  const body = el('div', {}, [
    el('h3', { text: 'Run this tool?' }),
    el('p', { class: 'note', text: `${backend}'s panel wants to call ${tool}.` }),
    el('pre', { class: 'block' }, [el('code', { text: pretty(args) })]),
  ]);
  const once = el('button', { type: 'button', class: 'primary', text: 'Allow once' });
  const always = el('button', { type: 'button', text: 'Allow for this panel' });
  const deny = el('button', { type: 'button', text: 'Deny' });
  dialog.append(body, el('div', { class: 'detail-actions' }, [once, always, deny]));
  document.body.append(dialog);

  return new Promise((resolve) => {
    const settle = (answer) => {
      dialog.close();
      dialog.remove();
      resolve(answer);
    };
    once.addEventListener('click', () => settle('once'));
    always.addEventListener('click', () => settle('always'));
    deny.addEventListener('click', () => settle('deny'));
    // Escape, or any other way a dialog closes, is a refusal. Defaulting the other way
    // would make walking away from the keyboard into a grant.
    dialog.addEventListener('cancel', () => settle('deny'));
    dialog.showModal();
  });
}

/**
 * The listing entry a panel's action names, or undefined. The listing is the authority.
 *
 * **Two spellings, and the backend's own is the one that matters.** A panel is written by a
 * backend, which knows its tools as `restart` and cannot know that a gateway in front of it
 * will list that as `panel__restart` -- the prefix is the *operator's* choice of server
 * name, made in `servers.yaml` long after the panel was written. So an action naming
 * `restart` is not a mistake to refuse; it is the only thing a panel author can say.
 *
 * The public spelling is accepted too, because a panel that learned its own tool's name
 * from the `tool-input` it was handed will echo that back, and both are the same tool.
 *
 * Resolution is scoped to `backend` rather than done by string surgery: `compose`-ing a
 * prefix onto a name would invent an entry that may not exist, and the point of looking in
 * the listing at all is that what comes back is something this gateway actually publishes.
 * The caller's owner check then still has something real to check.
 */
function toolEntry(name, backend) {
  const listed = port.state.listings.tools || [];
  return (
    listed.find((entry) => entry.name === name)
    ?? listed.find(
      (entry) => ownerOf('tools', entry) === backend && localName('tools', entry) === name,
    )
  );
}

/**
 * Run one `action`, or refuse it with a message the panel will show.
 *
 * Throws rather than returning a failure, because `panel_declarative.js` puts a thrown
 * message on the action and carries on -- a refusal is an ordinary outcome for a panel, not
 * a broken one.
 */
async function runAction(ledger, backend, { tool, arguments: args }) {
  // 1. Scope.
  if (String(tool).startsWith('gateway__')) {
    throw new Error('A panel may not call the gateway’s own tools.');
  }
  const entry = toolEntry(tool, backend);
  if (!entry) throw new Error(`No tool named ${tool} is listed.`);
  if (ownerOf('tools', entry) !== backend) {
    throw new Error(`${tool} is not a tool of this panel’s server.`);
  }
  // From here on it is the *listed* name, never the one the panel asked with. The two can
  // differ -- see `toolEntry` -- and everything downstream is either a wire call or a thing
  // a person reads: both have to say what this gateway will actually do. A consent dialog
  // naming `restart` while the socket carries `panel__restart` would be a prompt about a
  // different call from the one it authorises.
  const name = entry.name;

  // 3. Budget, checked before asking: there is no point asking about a call that is over
  // the ceiling anyway.
  if (ledger.calls >= MAX_CALLS) {
    throw new Error('This panel has made enough calls; reopen it to make more.');
  }
  const now = Date.now();
  if (now - ledger.lastAt < MIN_GAP_MS) throw new Error('Too fast — try that again in a moment.');

  // 2. Consent. Granted against the resolved name, so a panel cannot spend one grant twice
  // by spelling the same tool both ways.
  if (!ledger.granted.has(name)) {
    const answer = await askConsent({ backend, tool: name, args });
    if (answer === 'deny') throw new Error('Refused.');
    if (answer === 'always') ledger.granted.add(name);
  }

  ledger.calls += 1;
  ledger.lastAt = Date.now();

  // Through the same socket as a human's click, and onto its own card.
  const started = performance.now();
  const result = await port.state.mcp.request('tools/call', { name, arguments: args });
  port.pushCard({
    title: name,
    subtitle: `tools/call · from ${backend}’s panel`,
    request: { method: 'tools/call', params: { name, arguments: args } },
    body: renderToolResult(result),
    elapsedMs: performance.now() - started,
    raw: result,
    failed: !!result.isError,
  });
  return result;
}

/**
 * Read a tool's panel and push a card showing it.
 *
 * `result` is the call the panel is about. The document is fetched *after* the call, not
 * before, because a panel with no result to render is a panel with nothing in it -- and
 * because fetching it earlier would mean reading a backend's resource for a call the person
 * may never make.
 */
export async function openPanel({ entry, result, args, elapsedMs }) {
  const reference = panelFor(entry);
  const backend = ownerOf('tools', entry);
  const started = performance.now();

  let body;
  let raw = result;
  try {
    const read = await port.state.mcp.request('resources/read', { uri: reference });
    const contents = read.contents || [];
    // By `mimeType`, never by position. SEP-1865's `profile` parameter is what lets one
    // `ui://` carry both tiers, and a backend that publishes both is publishing them for
    // hosts to choose between -- so a host that took `contents[0]` would be choosing by
    // whatever order the backend happened to serialise.
    const declarative = contents.find((item) => isDeclarativePanel(item?.mimeType));
    const html = contents.find((item) => isHtmlPanel(item?.mimeType));
    const ledger = newLedger();
    if (declarative) {
      // Preferred when both are offered: it is DOM built by this page out of JSON, with
      // nothing executed and no frame to sandbox. The HTML tier is for the panels that
      // cannot be written that way, not the ones that can.
      const document_ = JSON.parse(declarative.text ?? 'null');
      body = renderPanel(document_, {
        result,
        onAction: (request) => runAction(ledger, backend, request),
      });
    } else if (html) {
      body = renderHtmlPanel({
        reference,
        call: { name: entry.name, arguments: args ?? {} },
        result,
        onAction: (request) => runAction(ledger, backend, request),
        mint: port.mint,
      });
    } else {
      body = el('div', {}, [
        el('p', {
          class: 'note',
          text: contents.length
            ? 'This panel is not one this build renders; showing the result instead.'
            : 'That panel could not be read; showing the result instead.',
        }),
        renderToolResult(result),
      ]);
    }
  } catch (error) {
    // A panel that fails to load must never cost you the result you already have.
    body = el('div', {}, [
      el('p', { class: 'note', text: `That panel could not be shown: ${error.message}` }),
      renderToolResult(result),
    ]);
  }

  port.pushCard({
    title: entry.name,
    subtitle: 'tools/call · panel',
    request: { method: 'tools/call', params: { name: entry.name } },
    body,
    elapsedMs: elapsedMs ?? performance.now() - started,
    raw,
    failed: !!result.isError,
  });
}
