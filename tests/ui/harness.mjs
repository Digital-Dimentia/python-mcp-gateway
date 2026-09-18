// A DOM to run the UI modules in, and the fixtures they are run against.
//
// `app.js` is the page's entry module: it has no initialiser to call, it wires the document
// up as it loads and then connects. So booting it is the whole job here -- put a real
// `index.html` behind the globals it reaches for, stub the one thing that would leave the
// process talking to a socket, and import it. What comes back is the module namespace,
// which is where its test seam lives (see the export block at the foot of `app.js`).
//
// What this is *not* is a browser. jsdom has no layout, so `scrollIntoView`,
// `setPointerCapture` and the geometry the column-resize code reads are stubs below. Every
// test in this directory is therefore about logic the DOM merely hosts -- which listing a
// pick narrows, which field a value lands in -- and never about how anything looks. A
// passing test here is not a promise that the page renders; `make run-dev` and
// `examples/zoo_server.py` are still what says that. See `src/mcp_gateway/webui.md`.

import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { JSDOM } from 'jsdom';

const UI = new URL('../../src/mcp_gateway_ui/', import.meta.url);

//: The URL the page is served from, so `location.host` gives `rpc.js` a socket address and
//: the `?key=` branch of `initialKey` has a query string to not find a key in.
const PAGE_URL = 'http://127.0.0.1:8765/ui/';

//: Node's own globals that a jsdom one must not replace. `performance` is the load-bearing
//: entry: jsdom's implementation *calls* `globalThis.performance.now()`, so handing it its
//: own is an infinite recursion the moment anything asks the time.
//:
//: The timers are here for exactly the same reason, found the same way: jsdom's
//: `setTimeout` runs `timerInitializationSteps`, which calls `globalThis.setTimeout` -- so
//: once jsdom's own is installed over Node's, the first `setTimeout` recurses until the
//: stack runs out. It stayed hidden for a while because nothing in the first three suites
//: ever scheduled anything; `rpc.js` schedules on every request timeout and every reconnect,
//: so `transport.test.mjs` and `shell.test.mjs` hit it immediately.
const SHADOWED = new Set([
  'window', 'globalThis', 'performance',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask',
]);

//: Everything the language itself puts on a global object, whichever realm it belongs to.
//: Computed rather than listed, so a newer V8's additions are covered without an edit.
const INTRINSICS = new Set(Object.getOwnPropertyNames(runInNewContext('this')));

//: One booted page per process, and it has to be that way: `app.js` reads `document` off
//: the global at call time, so a second window would leave the first suite's module talking
//: to the second suite's DOM -- and jsdom's own `performance` delegates to the global one,
//: which after a second boot is another jsdom's, forever. `node --test` gives each file its
//: own process, so the rule is one `boot()` per test file.
let booted = false;

/**
 * A socket that connects, then nothing. Enough for `state.mcp.state` to be inspectable.
 *
 * Handed to `rpc.setTransport` rather than written over `globalThis.WebSocket`, which is
 * what this used to be. The global stub worked, but it was a statement about the whole
 * realm made in order to reach one call site — and it could only ever silence the socket,
 * never drive it, because nothing called the `on*` handlers. The seam in `rpc.js` is the
 * honest version, and `transport.test.mjs` is what it bought.
 */
class SilentTransport {
  constructor(path) {
    this.path = path;
    this.readyState = 0;                 // CONNECTING, and it stays there
    this.sent = [];
  }

  send(data) { this.sent.push(data); }

  close() {}
}

/**
 * A booted page.
 *
 * `ui` is `app.js`'s exports, `window`/`document` the DOM it is wired to. Nothing needs
 * tearing down: the jsdom window is garbage once the test drops it, and the stub socket
 * holds no timer.
 */
export async function boot({ shell = false } = {}) {
  if (booted) {
    throw new Error('boot() is once per process — put this suite in its own test file');
  }
  booted = true;
  const html = readFileSync(new URL('index.html', UI), 'utf8');
  const dom = new JSDOM(html, { url: PAGE_URL, pretendToBeVisual: true, runScripts: 'outside-only' });
  const { window } = dom;

  // `theme.js` is a classic script in the head, and `app.js` reads `window.__theme` as it
  // loads. jsdom does not run `<script type="module">`, so the module graph is imported
  // below instead -- but this one has to run the way the page runs it.
  window.eval(readFileSync(new URL('theme.js', UI), 'utf8'));

  // No layout, so these do not exist. They are called for their visual effect only, and
  // every one of them is called on a path a test here does exercise.
  window.Element.prototype.scrollIntoView = function scrollIntoView() {};
  window.Element.prototype.setPointerCapture = function setPointerCapture() {};
  window.Element.prototype.releasePointerCapture = function releasePointerCapture() {};
  if (!window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
    window.HTMLDialogElement.prototype.close = function close() { this.open = false; };
  }

  // The modules are plain ES modules loaded by Node, not by jsdom, so what they see as
  // `document` is whatever `globalThis` carries. Hand them the window's own properties --
  // every one of them except the language itself. A jsdom window built with a script
  // context carries its realm's `Object`, `Array` and `Promise` too, and copying *those*
  // over Node's would put two realms' intrinsics in one module graph; `new Event(...)` has
  // to come from jsdom, but `Object.defineProperty` must not.
  for (const key of Object.getOwnPropertyNames(window)) {
    if (INTRINSICS.has(key) || SHADOWED.has(key)) continue;
    try {
      globalThis[key] = window[key];
    } catch {
      // A handful of Node's own globals are read-only accessors. None is a DOM API.
    }
  }
  globalThis.window = window;

  // The desktop shell, faked. `tauri-transport.js` looks for `__TAURI_INTERNALS__` as it
  // loads and installs its own transport only if it is there, so this has to be in place
  // *before* `app.js` pulls that module in -- which is the same ordering the real window
  // has, where the runtime injects these before any of our script runs.
  const host = shell ? fakeTauri() : null;
  if (host) {
    window.__TAURI_INTERNALS__ = {};
    window.__TAURI__ = host.api;
  }

  // Before `app.js`, which connects as it loads. Both modules come out of one registry, so
  // setting it here is setting it for the graph `app.js` is about to pull in. Skipped in
  // shell mode, where the point is to let the real shim install the real thing.
  const rpc = await import(new URL('rpc.js', UI).href);
  if (!shell) rpc.setTransport((path) => new SilentTransport(path));

  const ui = await import(new URL('app.js', UI).href);
  return { ui, window, document: window.document, dom, host };
}

/**
 * Enough of Tauri's injected API for `tauri-transport.js` to run against.
 *
 * `invoke` records rather than dispatches, which is the whole assertion in `shell.test.mjs`:
 * what the window asks the host to do, and -- more to the point -- what it does not ask for.
 * `Channel` is the far side of the socket, so a test can push frames at the page.
 */
export function fakeTauri() {
  const calls = [];
  const channels = [];
  const listeners = new Map();

  //: The settings the host would have read out of `connection.json`. Held here so the
  //: Connection screen's save really does round-trip through something, and so a test can
  //: start the page off in remote mode.
  let connection = { version: 1, mode: 'local', destination: '', localPort: 0, remotePort: 8765 };
  //: What the next `conn_save` should do. A string makes it reject with that message, which
  //: is how the host reports a destination it will not run.
  let refuseSave = null;

  class Channel {
    set onmessage(handler) { this._handler = handler; }
    get onmessage() { return this._handler; }
  }

  const api = {
    core: {
      invoke(command, args) {
        calls.push({ command, args });
        if (command === 'gw_open') channels.push(args.onFrame);
        if (command === 'conn_settings') return Promise.resolve({ ...connection });
        if (command === 'conn_save') {
          if (refuseSave) return Promise.reject(refuseSave);
          connection = { ...connection, ...args.settings };
          return Promise.resolve({ ...connection });
        }
        return Promise.resolve();
      },
      Channel,
    },
    // The window handle, for the one thing the page is allowed to do to its own frame:
    // wear the deployment's title. `setTitle` records like `invoke` does, so a test can ask
    // what the window was renamed to -- and, just as usefully, that it was renamed once.
    window: {
      getCurrentWindow: () => ({
        setTitle(title) {
          calls.push({ command: 'setTitle', args: { title } });
          return Promise.resolve();
        },
      }),
    },
    event: {
      listen(name, handler) {
        listeners.set(name, handler);
        return Promise.resolve(() => listeners.delete(name));
      },
    },
  };

  return {
    api,
    calls,
    /** Every `gw_open` so far, in order. */
    opened: () => calls.filter((c) => c.command === 'gw_open'),
    /** Every title the page has put on the window, in order. */
    titles: () => calls.filter((c) => c.command === 'setTitle').map((c) => c.args.title),
    /** Push a frame at the socket opened by the nth `gw_open`. */
    deliver(index, frame) { channels[index].onmessage(frame); },
    /** The settings the host is holding, as the page last left them. */
    connection: () => ({ ...connection }),
    /** Start the host off with different settings, before the screen asks for them. */
    setConnection(next) { connection = { ...connection, ...next }; },
    /** Make the next `conn_save` fail, the way a refused destination does. */
    refuseNextSave(message) { refuseSave = message; },
    /** Fire a `gateway-state` event at the page. */
    emit(name, payload) {
      const handler = listeners.get(name);
      if (handler) handler({ payload });
    },
  };
}

// ── Fixtures ───────────────────────────────────────────────────────────────────
//
// The zoo, as the gateway spells it. `examples/zoo_server.py` is the server these listings
// come from, and the cascade below -- continent, then country, then animal -- is the shape
// that broke twice: see python-mcp-gateway-6cm.

/** What `naming.encode_resource_uri` makes of one backend URI. See `naming.py`. */
export const gatewayUri = (server, local) => `mcpgw://${server}/${encodeURIComponent(local)}`;

/** One `resources/list` entry, in the gateway's namespaced spelling. */
export const resource = (server, local) => ({
  uri: gatewayUri(server, local),
  name: `${server}__${local}`,
});

/**
 * One `resources/templates/list` entry.
 *
 * The braces survive the encoding, which is not incidental: `naming.encode_resource_uri`
 * passes `safe='{}'` precisely so a client can still expand what it is given, and the
 * template form reads `templateVariables` off this gateway spelling rather than off the
 * backend's. A fixture that escaped them would name no variables at all.
 */
export const template = (server, local) => ({
  uriTemplate: `mcpgw://${server}/${encodeURIComponent(local).replace(/%7B/g, '{').replace(/%7D/g, '}')}`,
  name: `${server}__${local}`,
});

/**
 * Put a server's listings into `state` and select it.
 *
 * The UI's own namespacing is what `ownerOf`/`localName` undo, so the fixtures go in
 * namespaced and the column is left to read them back out -- which is part of what is
 * under test.
 */
export function selectServer(ui, server, { resources = [], templates = [] } = {}) {
  ui.state.selected = server;
  ui.state.listings = {
    tools: [],
    prompts: [],
    resources: resources.map((local) => resource(server, local)),
    templates: templates.map((local) => template(server, local)),
  };
}

/** A body in shape 1: a Schema enum fragment, optionally naming where its values go. */
export function enumBody(values, { readOne = null, narrows = null, labels = null } = {}) {
  const body = { enum: values };
  if (labels) body.enumNames = labels;
  if (readOne) body.readOne = readOne;
  if (narrows) body.narrows = narrows;
  return body;
}

/** What `resources/read` answers, wrapping one of the bodies above. */
export const readResult = (body) => ({ contents: [{ text: JSON.stringify(body) }] });

/**
 * Stand a ready `/mcp` socket up, answering `resources/read` from a table of bodies.
 *
 * Keyed by the gateway URI, which is the point: a read at a URI the table does not have is
 * a read the cascade should never have made, and it fails the test rather than inventing an
 * answer.
 */
export function mcpAnswering(ui, bodies) {
  const reads = [];
  ui.state.mcp = {
    state: 'ready',
    async request(method, params) {
      if (method !== 'resources/read') throw new Error(`unexpected ${method}`);
      reads.push(params.uri);
      if (!(params.uri in bodies)) throw new Error(`nothing published at ${params.uri}`);
      return readResult(bodies[params.uri]);
    },
  };
  return reads;
}

/** The groups on screen, as `variable@listing` plus why a pending one is pending. */
export const shape = (groups) => groups.map((group) => (
  group.pending
    ? `${group.variable || '?'}@pending:${group.pending}`
    : `${group.variable}@${group.listing}`
));

/** The value in the open form's `name` field, or `undefined` if it has no such field. */
export function fieldValue(document, name) {
  const field = document.querySelector(`#detail [data-field="${name}"]`);
  return field?.querySelector('input, select, textarea')?.value;
}

/**
 * Wait out a refresh that nothing handed you a promise for.
 *
 * `openVocabulary` and the pick handlers call `refreshVariables` and drop the promise --
 * they are click handlers, and a click handler has nobody to return to. `refreshVariables`
 * guards itself with a flag rather than queueing, so a second call made while the first is
 * still walking returns at once; a test that awaited only that second call would go on to
 * assert against a half-resolved column. So: let the one in flight finish.
 */
export async function settle(ui) {
  for (let i = 0; i < 100 && ui.refreshing; i += 1) {
    await new Promise((resume) => { setImmediate(resume); });
  }
  assertQuiet(ui);
}

function assertQuiet(ui) {
  if (ui.refreshing) throw new Error('the variables column never stopped resolving');
}
