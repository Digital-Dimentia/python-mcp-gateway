// The frame, and the wiring between the pieces inside it.
//
// Two sockets, deliberately. `/admin` answers what is configured and what is running;
// `/mcp` answers what those backends actually publish, through a real MCP handshake. The
// second half is not a convenience: a UI that asked `/admin` for a tool listing would be
// showing its own rendering of the catalogue, and the whole point of a test bench is that
// what you exercise here is byte-identical to what the model gets.
//
// What is left in this file is the part that is true on every screen: the two sockets and
// the gate in front of them, the deployment's branding, the server bar and the editor it
// drops, the log, the theme, and the registry that says which screen is showing. The Basics
// screen is coming out of here a piece at a time -- `screens/basics/variables.js` and `screens/basics/detail.js` so far,
// the rest under python-mcp-gateway-8mm -- and each piece is *handed* what it may touch,
// never given this file to import. The installs at the foot of `Go` are where that is done,
// and they are the only place either module is named twice.

import { AdminSocket, McpSocket, RpcError } from './rpc.js';
import {
  connectionApply, gatewayStatus, inShell, onGatewayState, setShellTitle,
} from './tauri-transport.js';
import { renderError, resultCard, pretty } from './render.js';
import { installClipboard, clipboardChanged } from './clipboard.js';
import { basename, formatDuration } from './format.js';
//: The screens. Each is one default export; the registry under `The screens` is what binds
//: one to the `<option>` that selects it.
import basicsScreen from './screens/basics/screen.js';
import aboutScreen from './screens/about/screen.js';
import connectionScreen from './screens/connection/screen.js';
//: The injectable values column, which is the first piece of the Basics screen to live
//: outside this file. It is handed what it may touch rather than importing it -- see
//: the `variables.install` call in `Go`, and the header of `screens/basics/variables.js`.
import * as variables from './screens/basics/variables.js';
//: The form a primitive opens into. It is handed the frame it needs, and hands back the
//: eight functions the column above needs to write into whatever form is open -- so this
//: file wires the two together and implements neither. See both modules' headers.
//: The left column: what the selected server publishes, and the row that opens the panel.
//: The middle column. It takes no port: see its header for why it is the one that does not.
import * as results from './screens/basics/results.js';
import * as primitives from './screens/basics/primitives.js';
import * as detail from './screens/basics/detail.js';


//: The gateway's own meta-tools live under this name and have no backend behind them, so
//: they appear in the header's server row as a synthetic entry rather than going unreachable.
const ADMIN_PREFIX = 'gateway';

const KEY_STORAGE = 'mcp-gateway-ui.key';

//: What the header wears before any socket has answered, and what it goes back to when a
//: reload removes the `branding:` block. Read off the markup rather than repeated here, so
//: `index.html` stays the one place the stock mark and name are written down.
const DEFAULT_BRAND_TITLE = 'MCP Gateway';
const STOCK_ICON = document.getElementById('brand-icon')?.getAttribute('src') || 'logo.svg';

//: The title the desktop window is currently wearing, so a refresh that changed nothing
//: does not cross the IPC boundary. Seeded from the markup rather than left null, because
//: the bundle's own window title is built from the same string: an unbranded gateway must
//: not rename the frame to what it is already called.
let shellTitle = document.title;

//: How many times a socket that has *never* connected may retry before the UI stops and
//: leaves the gate standing. Enough to ride out a daemon that is still starting; few enough
//: that a page parked on a wrong key is not a background 401 generator.
const RETRIES_BEFORE_GATE = 3;

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
};

// ── State ──────────────────────────────────────────────────────────────────────

const state = {
  key: null,
  admin: null,
  mcp: null,
  backends: [],
  config: { servers: {} },
  missing: {},
  //: `admin.secrets.keys`: names, and where each resolved from. `providers` is empty for a
  //: deployment with no `secrets:` block, which is what keeps About's card off the page.
  secrets: { keys: [], origins: {}, providers: [] },
  status: null,
  selected: null,          // backend name, or the ADMIN_PREFIX pseudo-entry
  kind: 'tools',
  listings: { tools: [], prompts: [], resources: [], templates: [] },
  item: null,
  everConnected: { admin: false, mcp: false },
  //: The desktop host's last full answer -- the phase it is in, the sentence that goes with
  //: it, and the tail of the child's stderr. Null in a browser. The Connection screen reads
  //: it; the gate is driven from the same payload as it arrives.
  shell: null,
  //: What the desktop host says about *where* this gateway is: local, or a machine reached
  //: through an SSH forward. Null in a browser and until the host's first answer.
  //:
  //: Deliberately not `admin.status.bind`. That is the daemon's own answer and a remote one
  //: says `127.0.0.1:8765` exactly as a local one does -- so the single field that looks
  //: like it says which machine you are driving is the one field that cannot. Only this
  //: process's host knows, and this is where it lands.
  connection: null,
};

/** ` on build-box`, or nothing at all. The suffix every warning about a remote gateway is built from. */
function whereSuffix() {
  const label = state.connection?.mode === 'remote' ? state.connection.label : null;
  return label ? ` on ${label}` : '';
}

//: Which screen the content panel is showing. Chrome rather than gateway truth -- the same
//: reason `openMenu` is not in `state` -- but at module scope so the refreshes can ask
//: whether the screen they would repaint is the one on the glass. Null until the selector
//: is wired at the foot of this file; nothing that reads it can run before then, because
//: every reader is downstream of a socket answering.
let activeScreen = null;

// ── Access key ─────────────────────────────────────────────────────────────────

function initialKey() {
  // In the desktop shell there is no key here and there never will be: the host mints one
  // per launch, keeps it in Rust, and opens both sockets itself. Returning early is not
  // just a shortcut -- it also means the shell never writes a secret into `localStorage`,
  // even on an origin where the browser build once did.
  if (inShell) return null;

  const params = new URLSearchParams(location.search);
  const fromUrl = params.get('key');
  if (fromUrl) {
    // Out of the address bar immediately. A key in a URL is already the wrong carrier;
    // leaving it there puts it in history, in the next bookmark, and in the referrer of
    // anything the page ever links to.
    params.delete('key');
    const query = params.toString();
    history.replaceState(null, '', location.pathname + (query ? `?${query}` : ''));
    try { localStorage.setItem(KEY_STORAGE, fromUrl); } catch { /* private window */ }
    return fromUrl;
  }
  try { return localStorage.getItem(KEY_STORAGE) || null; } catch { return null; }
}

// What the overlay is currently saying. Three modes, and the distinction between the first
// two is the whole point:
//
//   connecting  a splash. Nothing is wrong; the gateway has not answered *yet*.
//   blocked     the key is missing or wrong, and there is a field to fix it.
//   failed      it will not start, and here is what it said.
//
// The bug this replaced: the page painted, the shell's first `gw_open` answered "the
// gateway is not listening yet" because a cold interpreter takes a second to import, and
// the socket's `closed` handler wrote "This gateway may require an access key" over a
// window that was simply still starting. Then it connected and the message vanished. Every
// launch looked like a failure that fixed itself.
//
// So the modes are ranked, and `showGate` will not let a lower-ranked writer overwrite a
// higher-ranked one. A socket closing is only ever evidence of `connecting` until the
// retries run out; the shell's own supervisor is the only thing that can say `failed`.
const GATE_RANK = { connecting: 0, blocked: 1, failed: 2 };

const GATE_TITLES = {
  connecting: 'Starting…',
  blocked: 'Access key required',
  failed: 'The gateway could not start',
};

let gateMode = 'connecting';

//: Set when somebody presses "Connection settings" on a failed gate, and cleared the moment
//: anything works again. Without it the next `gateway-state` -- which arrives within the
//: second, still failing -- would put the panel straight back over the screen they just
//: asked to see.
let gateSuppressed = false;

function showGate(message, mode = 'blocked', title = GATE_TITLES[mode]) {
  const gate = $('gate');
  //: Deliberately checked before the rank guard: the suppression is a person's decision and
  //: outranks everything the host has to say until it is lifted.
  if (gateSuppressed && mode !== 'connecting') return;
  if (gateSuppressed) gateSuppressed = false;
  // A late socket close must not drag a `failed` panel back to a splash. Equal ranks do
  // update, so a second `restarting` can refresh its attempt count.
  if (!gate.hidden && GATE_RANK[mode] < GATE_RANK[gateMode]) return;
  gateMode = mode;
  gate.dataset.mode = mode;
  gate.hidden = false;
  //: `failed` covers two different failures now -- a daemon that would not start, and a
  //: tunnel that would not open -- and they want different sentences above the same message.
  $('gate-title').textContent = title;
  $('gate-message').textContent = message;

  // The key field exists for exactly one mode. Under the shell it never appears at all:
  // there is no key to ask a human for -- the host mints one and spends it itself -- so a
  // socket that will not open is the host's problem and not the user's.
  const asking = mode === 'blocked' && !inShell;
  $('gate-key-field').hidden = !asking;
  const submit = $('gate-submit');
  submit.textContent = asking ? 'Connect' : 'Retry';
  // Nothing to retry while it is still coming up, and a button that does nothing is worse
  // than no button.
  submit.hidden = mode === 'connecting';

  //: The escape hatch. This panel covers the header, and the header is where the Connection
  //: screen is chosen -- so a mistyped destination would otherwise hide the only control
  //: that could fix it. Shown for a failed remote connection and nothing else.
  $('gate-settings').hidden = !(inShell && mode === 'failed' && state.connection?.mode === 'remote');

  if (asking) $('gate-key').focus();
}

function hideGate() {
  $('gate').hidden = true;
  gateMode = 'connecting';
}

$('gate-settings').addEventListener('click', () => {
  gateSuppressed = true;
  hideGate();
  showScreen('connection');
});

$('gate-form').addEventListener('submit', (event) => {
  event.preventDefault();

  //: In remote mode "Retry" has to mean *re-dial the tunnel*. Rebuilding the sockets alone
  //: would aim them at a local port with nothing behind it, forever, on a backoff nobody
  //: can see.
  if (inShell && state.connection?.mode === 'remote') {
    hideGate();
    showGate('Reconnecting…', 'connecting');
    connectionApply().catch(() => {});
    return;
  }

  const value = $('gate-key').value.trim();
  state.key = value || null;
  try {
    if (value) localStorage.setItem(KEY_STORAGE, value);
    else localStorage.removeItem(KEY_STORAGE);
  } catch { /* private window */ }
  // Back to a splash rather than straight to the page. The sockets have not answered yet,
  // and the rank guard would otherwise keep a stale `blocked` or `failed` panel pinned in
  // front of a retry that is working.
  hideGate();
  showGate('Waiting for the gateway.', 'connecting');
  connect();
});

// ── Connection ─────────────────────────────────────────────────────────────────

function connect() {
  if (state.admin) state.admin.close();
  if (state.mcp) state.mcp.close();

  state.admin = new AdminSocket(state.key);
  state.mcp = new McpSocket(state.key);

  wire(state.admin, 'admin', $('pill-admin'), async () => {
    await refreshAdmin();
    startLogTail();
  });
  wire(state.mcp, 'mcp', $('pill-mcp'), () => primitives.refreshListings());

  state.admin.addEventListener('notify', (event) => {
    const { method, params } = event.detail;
    if (method === 'admin.logs') appendLog(params);
  });

  state.mcp.addEventListener('notify', (event) => {
    const { method } = event.detail;
    // The gateway relays a backend's `list_changed` upward. Refetching on it is the whole
    // reason the notification exists; a UI that ignores it shows a listing that was true
    // once.
    if (method.startsWith('notifications/') && method.endsWith('list_changed')) primitives.refreshListings();
  });

  state.admin.connect();
  state.mcp.connect();
}

function wire(socket, name, pill, onReady) {
  socket.addEventListener('state', (event) => {
    const { state: next, reason } = event.detail;
    pill.dataset.state = next;
    pill.title = reason ? `${socket.path}: ${reason}` : socket.path;
    if (next === 'ready') {
      state.everConnected[name] = true;
      hideGate();
      onReady();
    } else if (next === 'closed' && !state.everConnected[name]) {
      // A browser will not say *why* a socket failed — a 401 and a refused connection are
      // the same event here. So this cannot claim the key is wrong. It offers the field on
      // a failure that happened before any successful connection, which is the closest
      // honest thing to do.
      //
      // But not on the first close, and never in the shell. A daemon that is still starting
      // refuses connections for a second or two, which is indistinguishable from a wrong
      // key at this level and is overwhelmingly the commoner case at page load. So the
      // early closes are a splash, and only an exhausted retry budget is allowed to accuse
      // the key. In the shell the question never arises: the host holds the key, and its
      // own state events say what is really happening.
      if (inShell || socket.attempt < RETRIES_BEFORE_GATE) {
        showGate('Waiting for the gateway.', 'connecting');
      } else {
        showGate(state.key
          ? 'Could not open a socket. The access key may be wrong, or the daemon may be down.'
          : 'Could not open a socket. This gateway may require an access key.', 'blocked');
      }
      // ...and then stop trying. A socket that has never once connected is not waiting out
      // a blip, it is being refused, and a page left open on the gate would otherwise
      // reconnect on a backoff forever — a 401 every few seconds against the daemon, and a
      // console nobody can read anything else in. Three attempts first, so a browser opened
      // a moment before the daemon finished starting still finds it. The Connect button
      // builds fresh sockets, so this costs one click and nothing else.
      //
      // Not in the shell. Every word of the reasoning above is about a *browser*: a page
      // parked on a wrong key, generating a 401 every few seconds. Here there is no key to
      // be wrong, and a closed socket means the host is restarting the gateway -- which
      // recovers on its own, in seconds. Stopping after three tries would leave a window
      // that has to be relaunched every time the daemon reloads.
      if (!inShell && socket.attempt >= RETRIES_BEFORE_GATE) {
        socket.close();
        $('gate-message').textContent += ' Retrying stopped; press Connect to try again.';
      }
    }
    updateMeta();
  });
}

// The footer in three groups, all of it from one `admin.status`. What was a `Gateway`
// `<details>` at the foot of the left column — duplicating the name, version and uptime
// the footer already carried, in the one column that has to stay readable while you work
// — is now spread across the bar by what each fact answers: the sockets say where, the
// centre says what, the right says which files, next to the buttons that re-read them.
function updateMeta() {
  updateSockets();
  updateFiles();

  const info = state.mcp?.serverInfo;
  const status = state.status;
  const bits = [];
  if (info) bits.push(`${info.name || 'gateway'} ${info.version || ''}`.trim());
  else if (status?.version) bits.push(`gateway ${status.version}`);
  if (status) bits.push(`up ${formatDuration(status.uptime_seconds)}`);

  //: Which machine, before what version and how long it has been up: the remote case is the
  //: one somebody needs to know before they read anything else on the page.
  const remote = state.connection?.mode === 'remote' ? state.connection.label : null;
  if (remote) bits.push(remote);

  //: Ambient, and read without attention -- see `:root[data-connection]` in the stylesheet.
  document.documentElement.dataset.connection = remote ? 'remote' : 'local';

  const meta = $('bar-meta');
  meta.replaceChildren();
  bits.forEach((text, i) => {
    if (i) meta.append(el('span', { class: 'bar-sep', text: '·', 'aria-hidden': 'true' }));
    meta.append(el('span', { text }));
  });

  //: A screen drawn from these payloads is repainted from the same place the bars are --
  //: and only while it is the one being looked at. A hidden screen is repainted when it is
  //: chosen, by `showScreen`.
  SCREEN_MODULES[activeScreen]?.refresh?.(state);
}

// A pill said only whether its socket was open. It now says the whole of what an operator
// needs to point a client at it: the address, the path, and how many clients are on that
// path. The count comes from `connections`, which the daemon enumerates from the server's
// links rather than from initialized sessions — so a client mid-handshake is counted,
// which is exactly when someone looks.
function updateSockets() {
  const status = state.status;
  const addr = status?.bind || '';

  //: The remote chip, before the two socket pills it qualifies. `bind` below is the address
  //: the daemon answers on *its own* machine, which in remote mode is not this one -- so
  //: without this the footer reads exactly like a local gateway's.
  const remote = state.connection?.mode === 'remote' ? state.connection.label : null;
  const chip = $('pill-remote');
  chip.hidden = !remote;
  if (remote) {
    $('remote-host').textContent = remote;
    chip.title = `This window is driving the gateway on ${remote}, through an SSH tunnel: `
      + `127.0.0.1:${state.connection.localPort} here → 127.0.0.1:`
      + `${state.connection.remotePort} there.`;
  }

  for (const [id, path] of [['pill-mcp', '/mcp'], ['pill-admin', '/admin']]) {
    const pill = $(id);
    const clients = (status?.connections || []).filter((c) => c.path === path).length;
    const addrEl = pill.querySelector('[data-addr]');
    addrEl.textContent = addr || '…';
    addrEl.hidden = !addr;
    const countEl = pill.querySelector('[data-count]');
    countEl.textContent = String(clients);
    countEl.title = `${clients} client${clients === 1 ? '' : 's'} on ${path}${whereSuffix()}`;
  }
}

// Both files live on the right, each one beside the action that re-reads it: the config
// names the Reload button — "Reload servers.yaml" names the file, where "Reload config"
// only named the act — and the env file sits beside it under its own label. Basenames,
// with the full path on the `title`: a footer has no room for two absolute paths, and the
// directory is the part nobody is reading them for.
function updateFiles() {
  const status = state.status;

  const label = $('reload-file');
  label.textContent = status?.config_path ? basename(status.config_path) : 'config';
  if (status?.config_path) label.title = `${status.config_path}${whereSuffix()}`;
  const paths = [status?.config_path, status?.env_path].filter(Boolean);
  //: Whose files. Reload has always re-read the *daemon's* two files; in remote mode those
  //: are on another machine, and the button that says so is cheaper than the surprise.
  $('btn-reload').title = paths.length
    ? `Re-read ${paths.join(' and ')}${whereSuffix()}`
    : `Re-read the config and env files${whereSuffix()}`;

  const field = $('env-field');
  field.hidden = !status?.env_path;
  if (status?.env_path) {
    const file = $('env-file');
    file.textContent = basename(status.env_path);
    file.title = `${status.env_path}${whereSuffix()}`;
  }
}

// ── What /admin says ───────────────────────────────────────────────────────────

async function refreshAdmin() {
  const [backends, config, status, missing, secrets] = await Promise.all([
    state.admin.request('admin.backends').catch(() => ({ backends: [] })),
    state.admin.request('admin.config.get').catch(() => ({ servers: {} })),
    state.admin.request('admin.status').catch(() => null),
    state.admin.request('admin.secrets.missing').catch(() => ({ servers: {} })),
    state.admin.request('admin.secrets.keys').catch(() => ({})),
  ]);
  state.backends = backends.backends || [];
  state.config = config;
  state.status = status;
  state.missing = missing.servers || {};
  state.secrets = {
    keys: secrets.keys || [],
    origins: secrets.origins || {},
    providers: secrets.providers || [],
  };
  renderBackends();
  applyBranding(status?.branding);
  updateMeta();
}

// The deployment's own name and mark, from `admin.status` and applied on every refresh —
// so editing `branding:` and pressing Reload shows the new logo without a restart.
//
// Every field is optional and every one falls back to what the markup already said, which
// is what makes an unbranded gateway byte-identical to what it was. The icon arrives as a
// `data:` URI rather than a URL because the desktop shell's CSP cannot fetch one; the
// daemon reads the configured file and inlines it. See branding.md.
function applyBranding(branding) {
  const title = branding?.title || DEFAULT_BRAND_TITLE;
  document.title = title;
  $('brand-title').textContent = title;
  // Not `setTitle` on every refresh unconditionally: the shell call is IPC, and the title
  // bar is one of the few things a person notices flickering.
  if (title !== shellTitle) {
    shellTitle = title;
    setShellTitle(title);
  }

  // A configured icon arrives as a `data:` URI, because the desktop shell cannot fetch a
  // URL for one; the stock mark is an asset beside this file, which both hosts can load.
  // Setting `src` to the same string twice is free -- the browser does not refetch -- so
  // there is no need to track which of the two is currently up.
  const mark = branding?.icon || STOCK_ICON;
  $('brand-icon').src = mark;
  $('brand-favicon').href = mark;
}

// ── The server bar ─────────────────────────────────────────────────────────────
//
// One button per configured server, in the header. The button is deliberately austere --
// indicator and name, nothing else -- because a row of them is a row you read sideways,
// and a description on each would make that unreadable at four servers. The description,
// and every control that acts on the server, live in the menu the button drops.

//: Which server's menu is down, by name. Kept out of `state` because it is chrome, not
//: gateway truth, but kept at module scope so a refresh that rebuilds the bar underneath
//: an open menu puts it back rather than snapping it shut.
let openMenu = null;

function renderBackends() {
  const nav = $('servers');
  nav.replaceChildren();

  // First, always. The gateway's own meta-tools have no backend behind them -- without this
  // entry they are listed by `/mcp` and reachable from nowhere in the UI -- and it leads the
  // row rather than trailing it so the one entry that is always present is always in the
  // same place. Anywhere else and it moves every time a server is added or removed.
  nav.append(serverEntry({
    name: ADMIN_PREFIX,
    status: 'meta',
    enabled: true,
    description: 'the gateway itself',
  }, true));

  for (const backend of state.backends) nav.append(serverEntry(backend));

  if (!state.backends.length) {
    nav.append(el('span', { class: 'servers-empty', text: 'No servers configured.' }));
  }

  placeMenu();
}

// The menu is `position: fixed` -- see the note in style.css -- so its coordinates are
// this function's job. Measured after the bar is in the DOM, and nudged left when a menu
// hanging off a button near the right edge would otherwise run off the window.
function placeMenu() {
  const menu = $('servers').querySelector('.server-menu:not([hidden])');
  if (!menu) return;
  const pill = menu.parentElement.getBoundingClientRect();
  menu.style.top = `${pill.bottom + 4}px`;
  menu.style.left = '0px';
  const width = menu.getBoundingClientRect().width;
  menu.style.left = `${Math.max(8, Math.min(pill.left, window.innerWidth - width - 8))}px`;
}

function serverEntry(backend, meta = false) {
  const name = backend.name;
  const missing = meta ? [] : (state.missing[name] || []);
  const spec = meta ? {} : (state.config.servers?.[name] || {});
  const open = openMenu === name;

  const wrap = el('div', {
    class: `server${state.selected === name ? ' on' : ''}${backend.enabled ? '' : ' off'}`
      + (meta ? ' server-meta' : ''),
  });

  // Indicator and name, and that is the whole button.
  const button = el('button', {
    type: 'button',
    class: 'server-button',
    title: meta ? backend.description : (backend.error || backend.status),
  }, [
    el('span', { class: `dot dot-${backend.status}` }),
    el('span', { class: 'server-name', text: name }),
  ]);
  // Selecting a server and acting on one are two different intentions, so they are two
  // different buttons. This one only selects: choosing what the columns show should not
  // drop a menu over the columns you were choosing to look at.
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    openMenu = null;
    select(name);
  });
  wrap.append(button);

  const menu = el('div', { class: 'server-menu', id: `server-menu-${name}`, hidden: !open });
  const note = meta ? backend.description
    : (backend.description || spec.description || backend.command || '');
  if (note) menu.append(el('p', { class: 'server-desc', text: note }));
  if (backend.error) menu.append(el('p', { class: 'server-error', text: backend.error }));
  if (missing.length) {
    menu.append(el('p', { class: 'server-missing' }, [
      el('span', { text: 'Missing from gateway.env: ' }),
      el('code', { text: missing.join(', ') }),
    ]));
  }

  if (!meta) {
    const actions = el('div', { class: 'server-actions' });
    actions.append(action('Restart', () => admin('admin.backend.restart', { name })));
    actions.append(action(backend.enabled ? 'Disable' : 'Enable', () => admin('admin.backend.update', {
      name, changes: { enabled: !backend.enabled },
    })));
    actions.append(action('Edit', () => { openMenu = null; openEditor(name); }));
    actions.append(action('Remove', () => {
      if (!confirm(`Remove ${name} from servers.yaml${whereSuffix()}?`)) return null;
      openMenu = null;
      return admin('admin.backend.remove', { name });
    }, 'danger'));
    menu.append(actions);
  }

  // The caret carries the menu, and nothing else. It is added last because whether the
  // menu has anything in it is only known once it is built -- a server with no
  // description, no error and no actions would otherwise wear a caret that drops an
  // empty box.
  if (menu.childElementCount) {
    wrap.classList.add('server-split');
    const caret = el('button', {
      type: 'button',
      class: 'server-caret',
      'aria-haspopup': 'true',
      'aria-expanded': String(open),
      'aria-controls': menu.id,
      'aria-label': `Actions for ${name}`,
      title: `Actions for ${name}`,
    }, [el('span', { 'aria-hidden': 'true', text: '▾' })]);
    // Toggle only. The selection is the other button's business, so the menu of a server
    // you are not looking at can be opened without moving what the columns show.
    caret.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu = open ? null : name;
      renderBackends();
    });
    wrap.append(caret);
  }

  wrap.append(menu);
  return wrap;
}

// Attached once, at load: the button's own handler stops its click before it gets here, so
// this only ever sees clicks that landed somewhere else.
document.addEventListener('click', (event) => {
  if (openMenu === null) return;
  if (event.target.closest('.server-menu')) return;
  openMenu = null;
  renderBackends();
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  // Escape dismisses the tooltip whether or not a menu is open, and without swallowing the
  // key: WAI-ARIA asks that a tooltip be dismissible without moving focus, which matters
  // most to the person who cannot simply look past it.
  primitives.hideTooltip();
  if (openMenu === null) return;
  openMenu = null;
  renderBackends();
});

// A fixed menu does not follow its button on its own.
window.addEventListener('resize', placeMenu);
$('servers').addEventListener('scroll', placeMenu);

function action(text, handler, extra = '') {
  const button = el('button', { type: 'button', class: `ghost ${extra}`.trim(), text });
  button.addEventListener('click', async (event) => {
    event.stopPropagation();
    button.disabled = true;
    try {
      await handler();
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

/** Call an admin method, show whatever it says on the right, and refresh. */
async function admin(method, params) {
  const started = performance.now();
  try {
    const result = await state.admin.request(method, params);
    results.pushCard({
      title: method,
      subtitle: params?.name || '',
      request: { method, params: params || {} },
      body: adminResultBody(result),
      elapsedMs: performance.now() - started,
      raw: result,
    });
    await refreshAdmin();
    await primitives.refreshListings();
    return result;
  } catch (err) {
    results.pushCard({
      title: method,
      subtitle: params?.name || '',
      request: { method, params: params || {} },
      body: renderError(err instanceof RpcError ? err : { code: -32000, message: String(err) }),
      elapsedMs: performance.now() - started,
      raw: { error: { code: err.code, message: err.message, data: err.data } },
      failed: true,
    });
    return null;
  }
}

function adminResultBody(result) {
  const parts = [];
  // The writer's warnings are the one thing in an admin answer a person must not scroll
  // past: a literal value in a committed file is how a credential gets published.
  for (const warning of result?.warnings || []) {
    parts.push(el('p', { class: 'banner banner-warn', text: warning }));
  }
  parts.push(el('pre', { class: 'block' }, [el('code', { text: pretty(result) })]));
  return el('div', { class: 'result-body' }, parts);
}

// Which server the whole screen is about. The server bar's action rather than any one
// column's: it changes what every column below is showing, so it belongs where the buttons
// that trigger it are.
function select(name) {
  state.selected = name;
  state.item = null;
  renderBackends();
  primitives.render();
  variables.refreshVariables();
}

// ── Logs ───────────────────────────────────────────────────────────────────────

const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
let logEntries = [];

async function startLogTail() {
  try {
    const result = await state.admin.request('admin.logs.tail', {});
    logEntries = result.backlog || [];
    renderLog();
  } catch {
    // A daemon with no log stream is a daemon, not a failure. The pane stays empty.
  }
}

function appendLog(entry) {
  logEntries.push(entry);
  if (logEntries.length > 500) logEntries = logEntries.slice(-500);
  renderLog();
}

function renderLog() {
  const floor = LEVELS[$('log-level').value] || 20;
  const list = $('log');
  list.replaceChildren();
  let shown = 0;
  for (const entry of logEntries) {
    if ((LEVELS[entry.level] || 0) < floor) continue;
    shown += 1;
    list.append(el('li', { class: `log-${(entry.level || '').toLowerCase()}` }, [
      el('span', { class: 'log-time', text: new Date((entry.time || 0) * 1000).toLocaleTimeString() }),
      el('span', { class: 'log-level', text: entry.level || '' }),
      el('span', { class: 'log-message', text: entry.message || '' }),
    ]));
  }
  //: An empty <ol> in an auto-height drawer collapses to a sliver of chrome with nothing
  //: in it, which looks broken rather than quiet.
  if (!shown) list.append(el('li', { class: 'empty', text: 'Nothing at this level yet.' }));
  $('log-count').textContent = String(shown);
  list.scrollTop = list.scrollHeight;
}

$('log-level').addEventListener('change', renderLog);
$('btn-log-clear').addEventListener('click', () => { logEntries = []; renderLog(); });

// ── The log drawer ─────────────────────────────────────────────────────────────

// Open is a class, not `hidden`: the panel stays laid out and slides down behind the
// footer, so it has a height to animate to and the list keeps its scroll position between
// visits. See the `.drawer` rules in style.css.

function setLogOpen(open) {
  const drawer = $('log-drawer');
  if (open) {
    // Size it once, here. Clearing the pin first lets it lay out against its content --
    // and against the min/max in the stylesheet -- and reading `offsetHeight` both forces
    // that layout and yields the number to freeze. From here on the list scrolls inside a
    // panel that no longer moves; the pin is dropped on the *next* open, not on close, so
    // the drawer keeps its height all the way down through the closing slide.
    drawer.style.height = '';
    drawer.style.height = `${drawer.offsetHeight}px`;
  }
  drawer.classList.toggle('on', open);
  $('btn-log').setAttribute('aria-expanded', String(open));
  if (open) {
    const list = $('log');
    list.scrollTop = list.scrollHeight;
    document.addEventListener('click', closeLogOnOutsideClick);
    document.addEventListener('keydown', closeLogOnEscape);
  } else {
    document.removeEventListener('click', closeLogOnOutsideClick);
    document.removeEventListener('keydown', closeLogOnEscape);
  }
}

// This listener is attached during the very click that opened the drawer, so it sees that
// click bubble up a moment later -- the `#btn-log` arm is what stops the drawer from
// closing itself the instant it opens.
function closeLogOnOutsideClick(event) {
  if (event.target.closest('#log-drawer, #btn-log')) return;
  setLogOpen(false);
}

function closeLogOnEscape(event) {
  if (event.key === 'Escape') setLogOpen(false);
}

$('btn-log').addEventListener('click', () => {
  setLogOpen($('btn-log').getAttribute('aria-expanded') !== 'true');
});

// ── The screens ────────────────────────────────────────────────────────────────
//
// The header and the footer are the frame and do not change: they are the gateway -- the
// servers, the sockets, the log, the two files it reads -- and not a view of it. What the
// selector swaps is the content panel between them.
//
// The register of screens is the `<option>` list in `index.html`, read here rather than
// repeated: the control, the sections and the stylesheet's rules are three places a screen
// already has to be written down, and a fourth list in JavaScript would be the one that
// falls out of step. `theme.js` restores the stored name before the first paint and knows
// none of them; this is where a name that is no longer a screen is caught, because the body
// is parsed by the time a module runs and the options can be looked at.
//
// A screen is one default-exported object -- `{ id, refresh(state), show() }` -- and this is
// the registry of them. The contract is written out at the head of `screens/about/screen.js`; what
// matters here is that dispatch is a lookup rather than a branch, so adding a screen touches
// this object and nothing else in this file.
//
// `refresh` is *handed* the state rather than importing it. A screen that reached back into
// this module's variables would be a cycle on paper and a knot in practice; a screen that is
// a renderer of what it is given can be moved, tested and deleted on its own.
//
// Every screen renders on being shown rather than on every refresh. Basics is the work
// surface and the rest are pages about it; keeping a hidden page up to date is work nobody
// is looking at.

const screenSelect = $('screen-select');
const SCREENS = [...screenSelect.options].map((option) => option.value);

const SCREEN_MODULES = {
  [basicsScreen.id]: basicsScreen,
  [aboutScreen.id]: aboutScreen,
  [connectionScreen.id]: connectionScreen,
};

//: Every option must name a module, and every module an option. Cheap, and it fires on the
//: one mistake this design invites -- a screen added to the markup and to the stylesheet but
//: never registered, which shows an empty section and no error at all.
for (const name of SCREENS) {
  if (!SCREEN_MODULES[name]) console.warn(`no module registered for screen ${name}`);
}

//: ...and the subset *this host* can show. A screen marked `data-shell-only` is one whose
//: whole subject is the desktop host -- the child process, the SSH tunnel -- and in a browser
//: it would be a page of controls wired to nothing. The option is *removed* rather than
//: disabled: a disabled option still reads as a thing you might one day be allowed to pick,
//: and in a browser there is no such day.
//:
//: `SCREENS` stays the full register above, so the two checks that matter -- every option
//: has a section, every option has a module -- still cover every screen this build has,
//: whichever host is running it.
const AVAILABLE = SCREENS.filter((name) => {
  const option = screenSelect.querySelector(`option[value="${name}"]`);
  if (!option?.hasAttribute('data-shell-only') || inShell) return true;
  option.remove();
  return false;
});

function showScreen(name, store = true) {
  const screen = AVAILABLE.includes(name) ? name : AVAILABLE[0];
  activeScreen = screen;
  screenSelect.value = screen;
  //: Storage is written by the gesture, not by the restore. Writing on the way in would
  //: make every load a write, and would pin whichever screen is first in the list as an
  //: explicit choice for someone who has never made one. The one exception is a stored name
  //: this build no longer has a screen for: the attribute `theme.js` put on <html> still
  //: names it, the stylesheet is answering it with Basics, and leaving the two saying
  //: different things is how a later screen with that name comes back from the dead.
  //: The second half also covers a name this host cannot show -- `connection`, stored by
  //: the desktop app and then met in a browser. Unlike a slug from a later build, the
  //: stylesheet *recognises* that name, so leaving the attribute alone would show an empty
  //: panel forever rather than falling through to Basics.
  if (store || (name && !AVAILABLE.includes(name))) window.__screen.set(screen);

  //: Both hooks, in this order: draw from the payloads that moved while the screen was
  //: away, then do whatever needed the screen to actually be laid out.
  const module = SCREEN_MODULES[screen];
  module?.refresh?.(state);
  module?.show?.();
}

screenSelect.addEventListener('change', () => showScreen(screenSelect.value));

showScreen(window.__screen.get(), false);

// ── The primitives column ──────────────────────────────────────────────────────
//
// In `screens/basics/primitives.js`, with the hover tooltip that belongs to its rows. It is handed the
// state, the two things it may do to the detail panel, and one callback for "the listings
// were re-read" -- because what else stands on a listing is this file's business, not a
// column's.

// ── The injectable values column ───────────────────────────────────────────────
//
// In `screens/basics/variables.js`, not here. It is the largest single piece of the Basics screen, and the
// first to be given a file of its own: the vocabularies a server publishes, the cascade
// between them, and what is picked. It reaches this file only through the port installed in
// `Go` at the foot of this one -- so the two directions are `variables.<name>()` from here,
// and nothing at all from there.

// ── The detail panel ───────────────────────────────────────────────────────────
//
// In `screens/basics/detail.js`, with the form port it exports and the fan-out that drives it. What is left
// here is the wiring: `installs` at the foot of this file hand it the frame and hand the
// column its port, composed out of this file's catalogue helpers and that file's form.

// ── The results column ─────────────────────────────────────────────────────────
//
// In `screens/basics/results.js`, which is handed nothing at all.

// ── The clipboard ──────────────────────────────────────────────────────────────
//
// What the two right-hand columns *are*, as data. The document itself is rendered by the
// daemon -- `clipboard.py` -- because `gateway__clipboard` on `/mcp` hands the model the
// same text, and two renderers of one document drift. See `clipboard.md`.

/** The Results column and the Injectable values column, in the order they are on screen. */
function clipboardSnapshot() {
  // Neither the selected server nor the bind address is in here: `clipboard.py` renders
  // neither, and the document is kept to evidence about the calls. The server name is in
  // every entry regardless -- tool names are `server__tool`.
  return {
    results: results.snapshot(),
    variables: variables.snapshot(),
  };
}

installClipboard({
  gather: clipboardSnapshot,
  // `state.admin.request` rather than the `admin()` helper above: that one pushes a result
  // card, and a publish that pushed a card would publish again forever.
  send: (method, params) => state.admin.request(method, params),
});

$('btn-reload').addEventListener('click', () => admin('admin.reload', {}));
$('btn-refresh').addEventListener('click', async () => { await refreshAdmin(); await primitives.refreshListings(); });

// ── The server editor ──────────────────────────────────────────────────────────

//: The daemon's own fallbacks, mirrored from `config.py`. The last resort only: the file's
//: own `defaults:` block wins where it sets a key, and `admin.config.get` reports that
//: block for exactly this reason. Shown as placeholders and never written, so a drift here
//: misleads about an unset field and cannot put a wrong value in `servers.yaml`.
const BUILTIN_DEFAULTS = { timeout: 30, startup_timeout: 20 };

/** What omitting `key` will actually mean, as text for a placeholder. */
function defaultFor(key) {
  const value = state.config?.defaults?.[key] ?? BUILTIN_DEFAULTS[key];
  return value === undefined || value === null ? undefined : String(value);
}

// Three columns, so the whole spec is visible at once rather than scrolled past: what
// the daemon runs, what it runs it with, and how it treats the result. The `name` field
// only exists when adding, and is prepended to the first column there.
//
// The fourth entry is a placeholder -- what the field means when you leave it alone --
// either a string, or a function called at open time for the ones that depend on the
// loaded config. For the keys `defaults:` can set that is the value actually in force; for
// the rest it is a worked example of the shape the field wants.
//
// Placeholders and not values, deliberately: a prefilled `30` is a `30` *written to
// servers.yaml*, which would quietly override the file's own `defaults:` block and pin the
// new server to today's number forever. Left blank the key is omitted (see
// `collectEditor`) and the fallback chain still runs.
const EDITOR_GROUPS = [
  ['Process', [
    ['command', 'text', 'The executable, e.g. npx or python', 'npx'],
    ['description', 'text', 'What this server is for', 'Read-only files under /srv'],
    ['args', 'lines', 'One argument per line', '-y\n@modelcontextprotocol/server-filesystem\n/srv'],
    ['cwd', 'text', 'Working directory (optional)', () => defaultFor('cwd') ?? "the daemon's own"],
  ]],
  ['Environment', [
    ['env', 'pairs', 'KEY=${SECRET_NAME}, one per line. Values live in gateway.env, never here',
      'API_KEY=${FILES_API_KEY}'],
    ['env_passthrough', 'lines', 'Variables inherited from the daemon, one per line', 'PATH\nHOME'],
  ]],
  ['Behaviour', [
    ['timeout', 'number', 'Per-request seconds', () => defaultFor('timeout')],
    ['startup_timeout', 'number', 'Spawn + initialize + first listing, in seconds',
      () => defaultFor('startup_timeout')],
    ['enabled', 'boolean', 'Spawn this backend'],
    ['required', 'boolean', 'A failure here is fatal to the daemon'],
  ]],
];
function openEditor(name) {
  const existing = name ? (state.config.servers?.[name] || {}) : {};
  const dialog = $('server-dialog');
  const form = $('server-form');
  form.replaceChildren();

  form.append(el('h2', {
    text: (name ? `Edit ${name}` : 'Add a server') + whereSuffix(),
  }));
  form.append(el('p', {
    class: 'note',
    text: 'This writes servers.yaml, which is committed. Credential values belong in '
      + 'gateway.env and are referenced here as ${NAME}; nothing in this dialog can read '
      + 'or write one.',
  }));
  //: The mistake this whole dialog invites when the gateway is somewhere else. Everything
  //: below is a path and a process on the *daemon's* machine, and every field here reads
  //: like it means this one.
  if (state.connection?.mode === 'remote') {
    form.append(el('p', {
      class: 'note warn',
      text: `The command, its arguments and its working directory are paths on `
        + `${state.connection.label}, not on this machine — and this writes that machine's `
        + `servers.yaml.`,
    }));
  }

  const inputs = new Map();
  const columns = [];
  for (const [heading, fields] of EDITOR_GROUPS) {
    const column = el('section', { class: 'editor-column' }, [
      el('h3', { class: 'editor-heading', text: heading }),
    ]);
    if (!name && columns.length === 0) {
      const input = el('input', {
        type: 'text', required: true, spellcheck: false, placeholder: 'files',
      });
      inputs.set('name', { kind: 'text', input });
      column.append(field('name', input, 'The namespace its tools appear under. No "__", "/" or ":".'));
    }
    for (const [key, kind, help, placeholder] of fields) {
      const hint = typeof placeholder === 'function' ? placeholder() : placeholder;
      column.append(field(key, editorInput(key, kind, existing[key], hint, inputs), help));
    }
    columns.push(column);
  }
  form.append(el('div', { class: 'editor-columns' }, columns));

  const problems = el('p', { class: 'banner banner-fail', hidden: true });
  const save = el('button', { type: 'button', class: 'primary', text: name ? 'Save' : 'Add' });
  const cancel = el('button', { type: 'button', class: 'ghost', text: 'Cancel' });
  form.append(problems, el('div', { class: 'detail-actions' }, [save, cancel]));

  cancel.addEventListener('click', () => dialog.close());
  save.addEventListener('click', async () => {
    let changes;
    try {
      changes = collectEditor(inputs);
    } catch (err) {
      problems.textContent = err.message;
      problems.hidden = false;
      return;
    }
    const target = name || changes.name;
    delete changes.name;
    const result = name
      ? await admin('admin.backend.update', { name: target, changes })
      : await admin('admin.backend.add', { name: target, spec: changes });
    if (result) dialog.close();
  });

  dialog.showModal();
}

// Builds the control for one spec key and registers it for collectEditor().
function editorInput(key, kind, value, placeholder, inputs) {
  let input;
  if (kind === 'lines') {
    input = el('textarea', { rows: 3, spellcheck: false, placeholder, value: (value || []).join('\n') });
  } else if (kind === 'pairs') {
    const pairs = Object.entries(value || {}).map(([k, v]) => `${k}=${v}`);
    input = el('textarea', { rows: 3, spellcheck: false, placeholder, value: pairs.join('\n') });
  } else if (kind === 'boolean') {
    input = el('input', { type: 'checkbox', checked: value ?? (key === 'enabled') });
  } else if (kind === 'number') {
    input = el('input', { type: 'number', step: '0.5', min: '0', placeholder, value: value ?? '' });
  } else {
    input = el('input', { type: 'text', spellcheck: false, placeholder, value: value ?? '' });
  }
  inputs.set(key, { kind, input });
  return input;
}

// A tickbox and the word naming it belong on one line: the word is not a caption over a
// control, it is what ticking the box *means*. So a boolean field lays its label beside the
// box rather than above it, and wires the two together -- which is also what makes the word
// clickable, something the stacked version never was.
let fieldSeq = 0;

function field(key, input, help) {
  const tickbox = input.type === 'checkbox';
  if (tickbox && !input.id) input.id = `field-${key}-${++fieldSeq}`;
  const label = el('label', {
    class: 'field-label',
    htmlFor: tickbox ? input.id : undefined,
  }, [el('span', { class: 'field-name', text: key })]);
  return el('div', { class: tickbox ? 'field field-tick' : 'field' }, [
    label,
    input,
    el('p', { class: 'field-help', text: help }),
  ]);
}

function collectEditor(inputs) {
  const out = {};
  for (const [key, { kind, input }] of inputs) {
    if (kind === 'boolean') {
      out[key] = input.checked;
    } else if (kind === 'lines') {
      const lines = input.value.split('\n').map((l) => l.trim()).filter(Boolean);
      out[key] = lines;
    } else if (kind === 'pairs') {
      const env = {};
      for (const line of input.value.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const at = trimmed.indexOf('=');
        if (at <= 0) throw new Error(`env line is not KEY=value: ${trimmed}`);
        env[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim();
      }
      out[key] = env;
    } else if (kind === 'number') {
      if (input.value.trim() === '') continue;
      out[key] = Number(input.value);
    } else {
      const value = input.value.trim();
      if (value === '' && key !== 'description') continue;
      out[key] = value;
    }
  }
  if ('name' in out && !out.name) throw new Error('a server needs a name');
  return out;
}

$('btn-add-server').addEventListener('click', () => openEditor(null));

// ── Theme ──────────────────────────────────────────────────────────────────────

// `theme.js` has already applied the stored mode by the time this module runs; all that
// is left is the button that cycles it. Three states, not two: dropping 'system' would
// mean a page that no longer follows a machine switching itself to dark at sunset.
const THEME_FACE = {
  system: { icon: '◐', label: 'Theme: follow system' },
  light:  { icon: '☀', label: 'Theme: light' },
  dark:   { icon: '☾', label: 'Theme: dark' },
};

function showTheme(mode) {
  const face = THEME_FACE[mode] || THEME_FACE.system;
  $('theme-icon').textContent = face.icon;
  $('btn-theme').title = `${face.label} — click to change`;   // the visible word is only 'Theme:'
  $('btn-theme').setAttribute('aria-label', face.label);
}

$('btn-theme').addEventListener('click', () => {
  const modes = window.__theme.MODES;
  const next = modes[(modes.indexOf(window.__theme.get()) + 1) % modes.length];
  window.__theme.set(next);
  showTheme(next);
});

showTheme(window.__theme.get());

// ── Go ─────────────────────────────────────────────────────────────────────────

// The injectable values column, handed exactly what it may touch. Written out here rather
// than reached for on the other side, because this object *is* the seam: it is the whole
// list of what the Basics screen's biggest piece may do to the rest of the page, and a
// reader who wants to know should find it in one place.
// The Basics screen's two extracted pieces, each handed exactly what it may touch.
//
// The column's port is *composed* here rather than implemented: what it needs to know about
// the catalogue comes from this file, and every way it may touch an open form comes from the
// panel that owns the form. Neither module can reach the other except through this object,
// and neither can reach back into this one at all.
primitives.install({
  state,
  openItem: detail.openItem,
  clearDetail: detail.clear,
  //: What stood on the old listings and has to be reconsidered. One callback rather than an
  //: import, so the column that re-read them does not have to know the other exists.
  listingsChanged: () => {
    variables.vocabularies.clear();
    variables.refreshVariables();
  },
});

detail.install({
  state,
  selectionChanged: primitives.render,
  pushCard: results.pushCard,
});

variables.install({
  state,
  ownListings: primitives.ownListings,
  ...detail.formPort,
});

state.key = initialKey();

// In the shell, the host knows things the page cannot: that the gateway is still importing,
// that it exited, that it is on its fourth restart, and what it last said on stderr. Without
// this the first launch is a blank gate for as long as a cold interpreter takes to start,
// and a `servers.yaml` the daemon refuses is a window that says "connecting" forever with
// the reason sitting unread in the host's log buffer.
if (inShell) {
  const paint = (status) => {
    if (!status) return;

    //: Stashed first and unconditionally, before any early return: the footer, the About
    //: card, the server editor and the Connection screen all read it, and they have to be
    //: right on a status that says everything is fine.
    const moved = JSON.stringify(status.connection) !== JSON.stringify(state.connection);
    state.shell = status;
    state.connection = status.connection;
    if (moved) {
      //: A different connection is a different question, so a gate somebody dismissed for
      //: the last one has no claim on this one.
      gateSuppressed = false;
      updateMeta();
    }
    //: The Connection screen is the one place that renders the phase rather than the gate's
    //: five-word summary of it, and a tunnel coming up emits several times a second.
    if (activeScreen === 'connection') SCREEN_MODULES.connection?.refresh?.(state);

    if (status.state === 'listening') return;  // the sockets speak for themselves
    const last = status.log?.length ? status.log[status.log.length - 1] : '';
    const remote = status.connection?.mode === 'remote';

    if (status.state === 'failed') {
      //: A tunnel that would not open is not "the gateway could not start" -- nothing was
      //: ever asked to start. The host's `detail` already carries the fix.
      showGate(
        status.detail || status.reason || last || `The gateway exited before it could serve${whereSuffix()}.`,
        'failed',
        remote ? 'Could not reach the remote gateway' : GATE_TITLES.failed,
      );
      return;
    }

    //: `detail` first, because the host says something true in every phase now -- including
    //: the one that is nobody's failure: the tunnel is open and the daemon over there is not
    //: running. That reads as a broken app unless it is spelt out.
    showGate(status.detail || {
      idle: 'Waiting for the gateway.',
      starting: 'Starting the gateway…',
      restarting: `The gateway stopped; restarting (attempt ${status.attempt})…`,
    }[status.state] || last || 'Waiting for the gateway.', 'connecting');
  };

  onGatewayState(paint);
  // Asked once as well as subscribed. An event only fires on a *change*, so a window that
  // finished loading after the supervisor's last transition would otherwise sit on the
  // generic splash with the host's real answer -- "on attempt 4", or why it failed --
  // already emitted and gone.
  gatewayStatus().then(paint).catch(() => {});
}

connect();

// ── The test seam ──────────────────────────────────────────────────────────────
//
// Inert in the browser: this module is the page's entry point, nothing imports it, and an
// `export` an importer never reads costs the page nothing. What it buys is `tests/ui/` --
// the same source the page runs, run against a jsdom document, because the two bugs that
// made the variables column worth testing were both logic no Python test can reach. See
// `src/mcp_gateway/webui.md` and python-mcp-gateway-6cm.
//
// Deliberately narrow. What is here is the resolver -- which vocabularies pair, which
// groups the cascade produces, which picks survive -- and the two directions of the fill
// path. Rendering, styling and the sockets are not here and are not tested: their value is
// in being looked at.
export {
  state,
  SCREENS,
  AVAILABLE,
  SCREEN_MODULES,
  showScreen,
  EDITOR_GROUPS,
  applyBranding,
  openEditor,

  clipboardSnapshot,
};

// The variables column's half, passed straight through rather than re-wrapped: these are
// live bindings, and `opened`, `liveKeys` and `refreshing` are all reassigned inside
// `screens/basics/variables.js` as a cascade resolves. A suite that imported a copy would see the value
// they had when the module loaded, forever.
export {
  refreshing,
  vocabularies,
  picks,
  opened,
  liveKeys,
  vocabularyPairs,
  vocabularyGroups,
  variableFor,
  valuesFrom,
  readVocabulary,
  refreshVariables,
  renderVariables,
  openVocabulary,
  closeVocabulary,
  pickFor,
  fillPick,
  applyPicks,
  livePicks,
  buryField,
  fannedFields,
  spread,
} from './screens/basics/variables.js';

//: The detail panel's share of the seam, passed straight through for the same reason.
export { openItem, controlFor, putValue, fillField } from './screens/basics/detail.js';

//: And the left column's, under the name the suites already call it by.
export { render as renderPrimitives, hideTooltip } from './screens/basics/primitives.js';
export { gatewayUri } from './naming.js';

//: And the middle column's.
export { pushCard, clearResults } from './screens/basics/results.js';
