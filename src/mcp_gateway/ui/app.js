// The three columns, and everything that moves between them.
//
// Two sockets, deliberately. `/admin` answers what is configured and what is running;
// `/mcp` answers what those backends actually publish, through a real MCP handshake. The
// second half is not a convenience: a UI that asked `/admin` for a tool listing would be
// showing its own rendering of the catalogue, and the whole point of a test bench is that
// what you exercise here is byte-identical to what the model gets.

import { AdminSocket, McpSocket, RpcError } from './rpc.js';
import { inShell, onGatewayState } from './tauri-transport.js';
import { buildForm, buildPromptForm, templateVariables, expandTemplate } from './schema_form.js';
import {
  renderToolResult, renderPromptResult, renderResourceResult, renderError, resultCard, pretty,
} from './render.js';

// Mirrors `naming.py`. A server name may contain neither, which is what makes a split on
// the first separator unambiguous — see naming.md.
const SEPARATOR = '__';
const RESOURCE_SCHEME = 'mcpgw';

//: The gateway's own meta-tools live under this name and have no backend behind them, so
//: they appear in the header's server row as a synthetic entry rather than going unreachable.
const ADMIN_PREFIX = 'gateway';

const KEY_STORAGE = 'mcp-gateway-ui.key';

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
  status: null,
  selected: null,          // backend name, or the ADMIN_PREFIX pseudo-entry
  kind: 'tools',
  listings: { tools: [], prompts: [], resources: [], templates: [] },
  item: null,
  everConnected: { admin: false, mcp: false },
};

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

function showGate(message) {
  $('gate-message').textContent = message;
  $('gate').hidden = false;
  // Under the shell the gate is a status panel, not a prompt. There is no key to ask a
  // human for -- a socket that will not open means the gateway is down or still starting,
  // which is the host's problem and not the user's -- so the field goes and the button
  // becomes a retry. The heading says what is actually true.
  if (inShell) {
    $('gate').querySelector('h2').textContent = 'Gateway not connected';
    $('gate-key').closest('label').hidden = true;
    $('gate-form').querySelector('button[type="submit"]').textContent = 'Retry';
    return;
  }
  $('gate-key').focus();
}

$('gate-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const value = $('gate-key').value.trim();
  state.key = value || null;
  try {
    if (value) localStorage.setItem(KEY_STORAGE, value);
    else localStorage.removeItem(KEY_STORAGE);
  } catch { /* private window */ }
  $('gate').hidden = true;
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
  wire(state.mcp, 'mcp', $('pill-mcp'), () => refreshListings());

  state.admin.addEventListener('notify', (event) => {
    const { method, params } = event.detail;
    if (method === 'admin.logs') appendLog(params);
  });

  state.mcp.addEventListener('notify', (event) => {
    const { method } = event.detail;
    // The gateway relays a backend's `list_changed` upward. Refetching on it is the whole
    // reason the notification exists; a UI that ignores it shows a listing that was true
    // once.
    if (method.startsWith('notifications/') && method.endsWith('list_changed')) refreshListings();
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
      $('gate').hidden = true;
      onReady();
    } else if (next === 'closed' && !state.everConnected[name]) {
      // A browser will not say *why* a socket failed — a 401 and a refused connection are
      // the same event here. So this cannot claim the key is wrong. It offers the field on
      // a failure that happened before any successful connection, which is the closest
      // honest thing to do.
      showGate(state.key
        ? 'Could not open a socket. The access key may be wrong, or the daemon may be down.'
        : 'Could not open a socket. This gateway may require an access key.');
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

  const meta = $('bar-meta');
  meta.replaceChildren();
  bits.forEach((text, i) => {
    if (i) meta.append(el('span', { class: 'bar-sep', text: '·', 'aria-hidden': 'true' }));
    meta.append(el('span', { text }));
  });
}

// A pill said only whether its socket was open. It now says the whole of what an operator
// needs to point a client at it: the address, the path, and how many clients are on that
// path. The count comes from `connections`, which the daemon enumerates from the server's
// links rather than from initialized sessions — so a client mid-handshake is counted,
// which is exactly when someone looks.
function updateSockets() {
  const status = state.status;
  const addr = status?.bind || '';
  for (const [id, path] of [['pill-mcp', '/mcp'], ['pill-admin', '/admin']]) {
    const pill = $(id);
    const clients = (status?.connections || []).filter((c) => c.path === path).length;
    const addrEl = pill.querySelector('[data-addr]');
    addrEl.textContent = addr || '…';
    addrEl.hidden = !addr;
    const countEl = pill.querySelector('[data-count]');
    countEl.textContent = String(clients);
    countEl.title = `${clients} client${clients === 1 ? '' : 's'} on ${path}`;
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
  if (status?.config_path) label.title = status.config_path;
  const paths = [status?.config_path, status?.env_path].filter(Boolean);
  $('btn-reload').title = paths.length
    ? `Re-read ${paths.join(' and ')}`
    : 'Re-read the config and env files';

  const field = $('env-field');
  field.hidden = !status?.env_path;
  if (status?.env_path) {
    const file = $('env-file');
    file.textContent = basename(status.env_path);
    file.title = status.env_path;
  }
}

const basename = (path) => String(path).split('/').pop() || String(path);

const formatDuration = (seconds) => {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
};

// ── What /admin says ───────────────────────────────────────────────────────────

async function refreshAdmin() {
  const [backends, config, status, missing] = await Promise.all([
    state.admin.request('admin.backends').catch(() => ({ backends: [] })),
    state.admin.request('admin.config.get').catch(() => ({ servers: {} })),
    state.admin.request('admin.status').catch(() => null),
    state.admin.request('admin.secrets.missing').catch(() => ({ servers: {} })),
  ]);
  state.backends = backends.backends || [];
  state.config = config;
  state.status = status;
  state.missing = missing.servers || {};
  renderBackends();
  updateMeta();
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
      if (!confirm(`Remove ${name} from servers.yaml?`)) return null;
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
  if (event.key !== 'Escape' || openMenu === null) return;
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
    pushCard({
      title: method,
      subtitle: params?.name || '',
      request: { method, params: params || {} },
      body: adminResultBody(result),
      elapsedMs: performance.now() - started,
      raw: result,
    });
    await refreshAdmin();
    await refreshListings();
    return result;
  } catch (err) {
    pushCard({
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

// ── The column widths ──────────────────────────────────────────────────────────
//
// `theme.js` owns the storage, the validation and the defaults, because a split restored
// from a deferred module lands after the first paint. This owns the gesture. The two
// gutters are the edges between the columns, and dragging one moves only the pair it
// divides -- their sum is held constant, so the third column does not shuffle sideways
// while you are still aiming at the second.
//
// Everything is measured in pixels and written back as `fr`. An `fr` value is a pure ratio,
// so a set of measured widths *is* a valid set of `fr` numbers: writing the measurements
// back reproduces the layout exactly, and keeps it proportional through a later window
// resize with no listener of our own. It is also the only sound basis for the arithmetic.
// Once a column is sitting on its floor the grid takes that track out of the flex
// distribution and shares the rest among the others, so the rendered widths stop following
// the stored ratio -- and a delta measured against the stored numbers would send the
// divider somewhere other than where the cursor is on the very first move.

const columnsEl = document.querySelector('.columns');
const colEls = [...columnsEl.querySelectorAll('.col')];
const gutterEls = [...columnsEl.querySelectorAll('.gutter')];

let colDrag = null;

/** The floor a drag clamps against, read off the stylesheet rather than copied from it. */
function columnFloor() {
  const raw = getComputedStyle(columnsEl).getPropertyValue('--col-min').trim();
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  if (raw.endsWith('rem')) return parseFloat(raw) * root;
  if (raw.endsWith('px')) return parseFloat(raw);
  return 16 * root;
}

const measureColumns = () => colEls.map((col) => col.getBoundingClientRect().width);

/** Widths as shares of 100, to a tenth: small, legible numbers that mean the same thing. */
function columnShares(widths) {
  const total = widths[0] + widths[1] + widths[2];
  return widths.map((w) => Math.round((w / total) * 1000) / 10);
}

/** A separator that moves is a widget, and a widget with no value announces nothing. */
function showColumnValues(widths) {
  gutterEls.forEach((gutter, i) => {
    const share = (widths[i] / (widths[i] + widths[i + 1])) * 100;
    gutter.setAttribute('aria-valuenow', String(Math.round(share)));
  });
}

/**
 * The move itself. `i` names the pair, `delta` is in pixels, and it is clamped *before* it
 * is applied rather than after: clamping the result would let an overshoot accumulate out
 * of sight, and the divider would then sit still for the width of that overshoot on the way
 * back instead of picking the cursor up where it left it.
 */
function moveColumnEdge(widths, i, delta, floor) {
  const low = floor - widths[i];
  const high = widths[i + 1] - floor;
  //: Too narrow for both floors at once -- only reachable in the band between their total
  //: and the breakpoint where the columns stack. There is no honest move, so make none.
  if (low > high) return null;
  const step = Math.min(Math.max(delta, low), high);
  const next = widths.slice();
  next[i] += step;
  next[i + 1] -= step;
  return next;
}

function applyColumnDrag() {
  if (!colDrag) return;
  const next = moveColumnEdge(
    colDrag.widths, colDrag.index, colDrag.x - colDrag.startX, colDrag.floor,
  );
  if (!next) return;
  colDrag.shares = columnShares(next);
  window.__columns.apply(colDrag.shares);
  showColumnValues(next);
}

function endColumnDrag() {
  if (!colDrag) return;
  if (colDrag.frame) cancelAnimationFrame(colDrag.frame);
  colDrag.frame = 0;
  //: A press that never moved is a click, not a resize. Landing it anyway would store the
  //: split the page happens to be showing, which looks like nothing at all and quietly
  //: turns the stylesheet's default into a pinned layout that only a reset undoes.
  if (colDrag.x !== colDrag.startX) applyColumnDrag();   // the last position, not the last frame
  //: Written once, at the end. Persisting per frame would be a hundred serialisations of a
  //: number nobody has settled on yet.
  if (colDrag.shares) window.__columns.save(colDrag.shares);
  colDrag.gutter.classList.remove('on');
  document.documentElement.classList.remove('col-resizing');
  colDrag = null;
}

function resetColumns() {
  window.__columns.reset();
  showColumnValues(measureColumns());
}

for (const gutter of gutterEls) {
  gutter.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    //: Cancelling a `pointerdown` suppresses the compatibility mouse events in some
    //: engines, and `dblclick` -- the reset gesture -- with them. So selection is suppressed
    //: by a class instead, and the second click of a double is caught here as well.
    if (event.detail === 2) { resetColumns(); return; }

    const widths = measureColumns();
    //: Zero widths mean the stacked layout, or a page not laid out yet. Neither has an edge.
    if (!widths.every((w) => w > 0)) return;

    colDrag = {
      gutter,
      index: Number(gutter.dataset.gutter),
      startX: event.clientX,
      x: event.clientX,
      widths,                 // measured once: re-measuring mid-drag reads back what this
      floor: columnFloor(),   // same drag just wrote, and creeps by a rounding error a frame
      frame: 0,
      shares: null,
    };
    //: Pointer capture rather than window listeners: the pointer leaves the gutter on the
    //: first pixel and may leave the window entirely, and capture is what guarantees the
    //: `pointerup` that ends the gesture and the `pointercancel` that abandons it.
    if (gutter.setPointerCapture) gutter.setPointerCapture(event.pointerId);
    gutter.classList.add('on');
    document.documentElement.classList.add('col-resizing');
  });

  gutter.addEventListener('pointermove', (event) => {
    if (!colDrag || colDrag.gutter !== gutter) return;
    colDrag.x = event.clientX;
    //: One write per frame. A pointer reports faster than the screen refreshes, and every
    //: write here relays out three columns of results.
    if (colDrag.frame) return;
    colDrag.frame = requestAnimationFrame(() => {
      if (!colDrag) return;
      colDrag.frame = 0;
      applyColumnDrag();
    });
  });

  gutter.addEventListener('pointerup', endColumnDrag);
  gutter.addEventListener('pointercancel', endColumnDrag);

  // Back to the stylesheet's split, and the stored one forgotten: a reset that left the old
  // numbers in storage would bring them back on the next reload.
  gutter.addEventListener('dblclick', resetColumns);

  // The same gesture for someone who tabbed here. Measured fresh each time, because a
  // keypress is a whole gesture rather than one frame of one.
  gutter.addEventListener('keydown', (event) => {
    if (event.key === 'Home') {
      event.preventDefault();
      resetColumns();
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();                      // otherwise the arrows scroll the column
    const step = (event.shiftKey ? 64 : 16) * (event.key === 'ArrowLeft' ? -1 : 1);
    const widths = measureColumns();
    if (!widths.every((w) => w > 0)) return;
    const next = moveColumnEdge(widths, Number(gutter.dataset.gutter), step, columnFloor());
    if (!next) return;
    window.__columns.save(columnShares(next));
    showColumnValues(next);
  });
}

//: A window that changes size under a live drag invalidates the widths that drag was
//: measured from. Ending it is both the cheap fix and what the gesture looks like anyway.
window.addEventListener('resize', endColumnDrag);

showColumnValues(measureColumns());

// ── The primitives column ──────────────────────────────────────────────────────

async function refreshListings() {
  if (state.mcp?.state !== 'ready') return;
  const capabilities = state.mcp.capabilities || {};
  const ask = (method, key, guard) => (guard === false
    ? Promise.resolve({ [key]: [] })
    : state.mcp.request(method).catch(() => ({ [key]: [] })));

  const [tools, prompts, resources, templates] = await Promise.all([
    ask('tools/list', 'tools'),
    ask('prompts/list', 'prompts', !!capabilities.prompts),
    ask('resources/list', 'resources', !!capabilities.resources),
    ask('resources/templates/list', 'resourceTemplates', !!capabilities.resources),
  ]);
  state.listings = {
    tools: tools.tools || [],
    prompts: prompts.prompts || [],
    resources: resources.resources || [],
    templates: templates.resourceTemplates || [],
  };
  renderPrimitives();
  // The listings changed, so the vocabularies behind them may have too.
  vocabularies.clear();
  refreshVariables();
}

/** The backend a listing entry belongs to. Mirrors naming.py, and only that. */
function ownerOf(kind, entry) {
  if (kind === 'tools' || kind === 'prompts') {
    const at = String(entry.name || '').indexOf(SEPARATOR);
    return at < 0 ? null : entry.name.slice(0, at);
  }
  const uri = String(entry.uri || entry.uriTemplate || '');
  const prefix = `${RESOURCE_SCHEME}://`;
  if (!uri.startsWith(prefix)) return null;
  return uri.slice(prefix.length).split('/')[0] || null;
}

/** The part after the namespace, which is what the backend itself called it. */
function localName(kind, entry) {
  if (kind === 'tools' || kind === 'prompts') {
    const at = String(entry.name || '').indexOf(SEPARATOR);
    return at < 0 ? entry.name : entry.name.slice(at + SEPARATOR.length);
  }
  const uri = String(entry.uri || entry.uriTemplate || '');
  const prefix = `${RESOURCE_SCHEME}://${ownerOf(kind, entry) || ''}/`;
  if (!uri.startsWith(prefix)) return uri;
  // The gateway percent-encodes the backend's own URI into one path segment.
  try { return decodeURIComponent(uri.slice(prefix.length)); } catch { return uri.slice(prefix.length); }
}

function entriesFor(kind) {
  const all = state.listings[kind] || [];
  if (!state.selected) return [];
  const needle = $('filter').value.trim().toLowerCase();
  return all
    .filter((entry) => ownerOf(kind, entry) === state.selected)
    .filter((entry) => !needle || JSON.stringify(entry).toLowerCase().includes(needle));
}

function select(name) {
  state.selected = name;
  state.item = null;
  renderBackends();
  renderPrimitives();
  refreshVariables();
}

function renderPrimitives() {
  $('primitives-title').textContent = state.selected || 'Primitives';
  for (const kind of Object.keys(state.listings)) {
    const count = state.selected
      ? (state.listings[kind] || []).filter((e) => ownerOf(kind, e) === state.selected).length
      : 0;
    document.querySelector(`[data-count="${kind}"]`).textContent = String(count);
  }

  const list = $('primitives');
  list.replaceChildren();
  if (!state.selected) {
    list.append(el('li', { class: 'empty', text: 'Select a server in the header.' }));
    $('detail').replaceChildren();
    return;
  }
  const entries = entriesFor(state.kind);
  if (!entries.length) {
    list.append(el('li', { class: 'empty', text: `No ${state.kind} for ${state.selected}.` }));
  }
  for (const entry of entries) {
    const id = entry.name || entry.uri || entry.uriTemplate;
    const row = el('li', {
      class: `primitive${state.item && itemId(state.item.entry) === id ? ' on' : ''}`,
    });
    const button = el('button', { type: 'button', class: 'primitive-main' }, [
      el('span', { class: 'primitive-name', text: localName(state.kind, entry) }),
      el('span', { class: 'primitive-note', text: entry.description || entry.title || '' }),
    ]);
    for (const badge of annotationBadges(entry)) button.append(badge);
    button.addEventListener('click', () => openItem(state.kind, entry));
    row.append(button);
    list.append(row);
  }
}

const itemId = (entry) => entry.name || entry.uri || entry.uriTemplate;

function annotationBadges(entry) {
  const a = entry.annotations || {};
  const badges = [];
  // Worth showing *before* the button is pressed: destructive is the one thing a person
  // needs to know about a tool they are about to fire at their own machine.
  if (a.destructiveHint) badges.push(el('span', { class: 'badge badge-danger', text: 'destructive' }));
  if (a.readOnlyHint) badges.push(el('span', { class: 'badge', text: 'read-only' }));
  if (a.idempotentHint) badges.push(el('span', { class: 'badge', text: 'idempotent' }));
  if (a.openWorldHint) badges.push(el('span', { class: 'badge', text: 'open-world' }));
  return badges;
}

document.querySelectorAll('#tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    state.kind = button.dataset.kind;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b === button));
    state.item = null;
    $('detail').replaceChildren();
    renderPrimitives();
  });
});

$('filter').addEventListener('input', renderPrimitives);

// ── The variables column ───────────────────────────────────────────────────────
//
// A server that publishes `zoo://animals/{id}` usually publishes `zoo://animals` beside it:
// a listing small enough to send whole, and a template for the per-member read that would
// not be. That pair is a *vocabulary* -- the set of values `id` may take -- and this column
// is those vocabularies, one group per variable, every value a button that fills the field
// it belongs in.
//
// The pairing is read off the URIs rather than guessed: a template's fixed prefix, up to
// its first `{`, is the listing's URI. That is also what keeps this column from reading the
// server's resources at large. `resources/read` is a call to a live backend -- `zoo://ticks`
// in the fixture exists precisely to prove a read can move state -- so a resource that pairs
// with no template is never fetched.

//: Bodies already read, keyed by the gateway's URI for them. Kept across selections, so
//: flipping between two servers does not re-read either one's listings; the Reread button
//: and a `list_changed` are what clear it.
const vocabularies = new Map();

//: What is picked, per vocabulary: `{variable, multi, values}`. Keyed by the listing's
//: gateway URI rather than by the variable name, because two servers -- or two listings on
//: one server -- may both name their variable `id`.
const picks = new Map();

//: The vocabularies asked for, by the listing's gateway URI, in the order they were added.
//:
//: Empty to begin with, and deliberately: this column reads live backends, and a column that
//: opened every vocabulary a server publishes would spend a read on each one before knowing
//: whether anybody wanted it -- and would fill itself with groups that have nothing in them
//: yet, which reads as clutter rather than as an offer. So it starts as a menu and becomes a
//: column as you choose from it.
let opened = [];

//: The keys the last resolve produced -- the groups actually on screen. A pick outside this
//: set is a pick nothing can show you or take back: a closed vocabulary's, or one made under
//: a continent you have since changed. Either way it must not go on multiplying the fan-out.
let liveKeys = new Set();

//: How deep a chain of `narrows` is followed. A server whose listings point at each other in
//: a circle is not a case to handle gracefully, but it is one that must not spin the column.
const MAX_CHAIN_DEPTH = 4;

//: Set while `refreshVariables` is walking, because a pick in a narrowing group now calls it
//: and the walk it would re-enter is already going to see that pick.
let refreshing = false;

//: Input types that parse what they are given, and so cannot be shown a `[a, b]` set: the
//: browser drops the text and leaves the field empty, without saying so.
const PARSED_INPUTS = new Set([
  'checkbox', 'radio', 'number', 'range', 'date', 'time', 'datetime-local', 'month', 'week',
  'color',
]);

//: How many expansions a template's `Expands to` line shows before it counts the rest.
const EXPANSIONS_SHOWN = 6;

//: How many calls a fan-out may start before it asks. Not a limit, a speed bump: six
//: animals crossed with four sizes is twenty-four calls at a live backend, and the person
//: who meant that should say so once.
const FAN_OUT_ASKS_ABOVE = 8;

//: The control in the detail panel that last had focus. The fallback for a value whose
//: variable names no field in the open form: the field you were last typing in is a better
//: guess than nothing, and it is the only sane target for a raw-JSON form.
let lastFocused = null;

//: The open form's send button and the word it wears when nothing is fanned out. Held here
//: so a pick made in this column can show, on the button, how many calls it just bought.
let sendControl = null;

$('detail').addEventListener('focusin', (event) => {
  const control = event.target.closest('input, select, textarea');
  lastFocused = control && !control.readOnly ? control : lastFocused;
});

/**
 * The vocabularies the selected server publishes, as
 * `[{variable, listing, template, uri}]` — `listing` and `template` in the backend's own
 * spelling, `uri` in the gateway's, because that is what `resources/read` takes.
 */
function vocabularyPairs() {
  if (!state.selected) return [];
  const mine = (kind) => (state.listings[kind] || [])
    .filter((entry) => ownerOf(kind, entry) === state.selected);

  const listings = new Map(mine('resources').map((r) => [localName('resources', r), r]));
  const pairs = [];
  for (const template of mine('templates')) {
    const spelling = localName('templates', template);
    const cut = spelling.indexOf('{');
    if (cut < 1) continue;
    // `zoo://animals/{id}` -> `zoo://animals`. The separator before the variable belongs to
    // the template, not to the listing's own URI.
    const prefix = spelling.slice(0, cut).replace(/[/#?&]+$/, '');
    const listing = listings.get(prefix);
    if (!listing) continue;
    const variable = templateVariables(spelling)[0];
    if (!variable) continue;
    pairs.push({ variable, listing: prefix, template: spelling, uri: listing.uri });
  }
  // One listing is one vocabulary, and therefore one group. Two templates can share a
  // fixed prefix -- `zoo://continents/{continent}/countries` and the longer one under it
  // both cut back to `zoo://continents` -- and both `vocabularies` and `picks` are keyed by
  // the listing's URI, so a second group on the same key would alias the first one's cache
  // and its picks. Keep the simplest pairing: fewest variables, then shortest, then
  // alphabetical, so the answer does not depend on listing order.
  //
  // Nothing is lost by dropping the others. A template that takes a value this listing does
  // not publish is not this listing's template; it is reached through the body of the one
  // that does, which is what `narrows` is for.
  const simpler = (a, b) => (
    templateVariables(a.template).length - templateVariables(b.template).length
    || a.template.length - b.template.length
    || a.template.localeCompare(b.template)
  );
  const simplest = new Map();
  for (const pair of pairs) {
    const held = simplest.get(pair.uri);
    if (!held || simpler(pair, held) < 0) simplest.set(pair.uri, pair);
  }
  return [...simplest.values()];
}

/**
 * A backend's own URI in the gateway's address space.
 *
 * Mirrors `naming.encode_resource_uri` (`naming.py`), and only that far: the gateway
 * percent-encodes the backend's URI into one path segment, and `decode_resource_uri`
 * unquotes it, so all that has to agree is the round trip -- not which characters each side
 * chose to escape.
 *
 * What it is for is the cascade. A listing reached through another listing's `narrows` is a
 * URI the *server* produced and no listing publishes, so there is no entry to read its
 * gateway spelling off. Minting one is safe because `resources/read` resolves a URI rather
 * than looking it up: `Catalogue.find_resource` decodes the namespace and hands the rest to
 * the backend, exactly as it would for a template the client expanded itself.
 */
function gatewayUri(local) {
  return `${RESOURCE_SCHEME}://${state.selected}/${encodeURIComponent(local)}`;
}

/**
 * The variable one group's values fill.
 *
 * Read off the body once there is one, because the body is the only thing that knows: a
 * listing names the template its values are spent on, and the variable is the first of that
 * template's the chain has not already bound. `zoo://continents/africa/countries` is spent
 * on a template naming `continent` *and* `country`, and the first of those is already
 * decided — it is in the URI — so this group is the `country` one.
 *
 * Falls back to the pairing's guess, which is all there is before the body arrives.
 */
function variableFor(group, read) {
  const bound = group.context || {};
  const spends = read?.narrows || read?.readOne;
  const named = spends && templateVariables(spends).find((name) => !(name in bound));
  return named || group.variable || null;
}

/**
 * Every group to draw, roots first and each child directly after its parent.
 *
 * A vocabulary whose body carries `narrows` does not publish its values: it publishes the
 * *template* of the listing that does, and which listing that is depends on what you picked
 * here. So this walks. A root comes from the pairing above; each deeper group is the parent
 * body's `narrows`, expanded with everything the chain has bound so far.
 *
 * The rule that makes the walk safe is the one the column already had: a listing is only
 * ever read at a URI something handed us. A root is named by a template's fixed prefix, and
 * a child by its parent's own body — so the column still never goes looking through a live
 * backend's resource space for something that might be a vocabulary.
 *
 * A child whose parent has no single pick is emitted `pending`: it is drawn, so you can see
 * that picking a continent is what will fill it, and it is neither read nor given a pick.
 */
function vocabularyGroups() {
  const groups = [];
  // Only what was asked for. A pairing nobody opened is an entry in the menu below, not a
  // group here and not a read.
  const available = new Map(vocabularyPairs().map((pair) => [pair.uri, pair]));
  opened = opened.filter((uri) => available.has(uri));
  const queue = opened.map((uri) => ({ ...available.get(uri), context: {}, depth: 0 }));

  while (queue.length) {
    const group = queue.shift();
    const read = group.pending ? null : vocabularies.get(group.uri);
    // Settled here rather than in the renderer, because the *next* link expands against it:
    // a group that thought it was still `continent` would write the country into the
    // continent's segment and read a URI nobody published.
    group.variable = variableFor(group, read);
    groups.push(group);
    if (group.pending || group.depth >= MAX_CHAIN_DEPTH || !read?.narrows) continue;

    // One pick, not several: a `narrows` buys one listing, and two continents' countries
    // merged would be a vocabulary the server never published. See `vocabularyGroup`.
    const pick = picks.get(group.uri);
    const chosen = pick && pick.values.size === 1 ? [...pick.values][0] : null;
    const child = { template: read.narrows, depth: group.depth + 1, parent: group.uri };
    if (chosen === null) {
      queue.push({ ...child, pending: 'pick', from: group.variable, context: group.context });
      continue;
    }

    const context = { ...group.context, [group.variable]: chosen };
    const unbound = templateVariables(read.narrows).filter((name) => !(name in context));
    if (unbound.length) {
      // Expanding now would leave a segment empty, which is a different URI from the one
      // meant — so the chain stops here rather than reading something nobody asked for.
      queue.push({ ...child, pending: 'unbound', from: unbound.join(', '), context });
      continue;
    }
    const listing = expandTemplate(read.narrows, context);
    queue.push({ ...child, listing, uri: gatewayUri(listing), context, variable: null });
  }

  // A pick outlives its group in exactly two ways, and one rule buries both: a country
  // picked under Africa once the continent is Asia, and anything picked in a vocabulary that
  // has since been closed. Left in `picks`, either would keep counting toward the send
  // button's `×n` with nothing on screen to explain the number.
  //
  // The bodies are *not* dropped with them. Those are a cache of what a live backend said;
  // re-opening a vocabulary, or going back to a continent, should cost no second read.
  liveKeys = new Set(groups.filter((group) => !group.pending).map((group) => group.uri));
  for (const [key, pick] of [...picks.entries()]) {
    if (liveKeys.has(key)) continue;
    picks.delete(key);
    buryField(pick);
  }
  return groups;
}

/**
 * Read whatever has not been read yet, then draw.
 *
 * A loop rather than one pass, because the cascade is only discoverable one layer at a
 * time: which listing sits under `africa` is a fact in the body of `zoo://continents`, so
 * that body has to be in hand before the child is even a URI. Each turn resolves what is
 * now knowable and reads it; the walk ends when a turn finds nothing new, which is at most
 * once per level.
 */
async function refreshVariables() {
  renderVariables();
  if (state.mcp?.state !== 'ready' || refreshing) return;
  refreshing = true;
  try {
    for (let level = 0; level <= MAX_CHAIN_DEPTH; level += 1) {
      const unread = vocabularyGroups()
        .filter((group) => !group.pending && !vocabularies.has(group.uri));
      if (!unread.length) break;
      for (const group of unread) vocabularies.set(group.uri, { loading: true });
      renderVariables();
      await Promise.all(unread.map(async (group) => {
        try {
          const result = await state.mcp.request('resources/read', { uri: group.uri });
          vocabularies.set(group.uri, readVocabulary(result));
        } catch (err) {
          vocabularies.set(group.uri, { values: [], error: err.message || String(err) });
        }
      }));
    }
  } finally {
    refreshing = false;
  }
  renderVariables();
}

/**
 * The values in a `resources/read` result.
 *
 * JSON only, and deliberately: a vocabulary has to be machine-readable to be one, and
 * splitting prose on newlines would turn every text resource into a list of garbage values.
 * Three shapes are understood, in the order a server is likely to publish them.
 */
function readVocabulary(result) {
  const contents = result?.contents || [];
  const text = contents.map((c) => c.text).find((t) => typeof t === 'string');
  if (text === undefined) {
    return { values: [], error: 'This listing has no text content to read values from.' };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return { values: [], error: 'This listing is not JSON, so no values can be read from it.' };
  }
  return valuesFrom(body);
}

function valuesFrom(body) {
  // 1. A JSON Schema enum fragment — what `zoo://animals` publishes, and the one shape that
  //    carries labels *and* names its own template. Neither `readOne` nor `narrows` is a
  //    Schema keyword; they are the listing saying where one of its values is spent, and
  //    they differ in what it buys: `readOne` a member, `narrows` another listing.
  if (body && typeof body === 'object' && Array.isArray(body.enum)) {
    const names = Array.isArray(body.enumNames) ? body.enumNames : [];
    return {
      values: body.enum.map((value, i) => choice(value, names[i])),
      readOne: typeof body.readOne === 'string' ? body.readOne : null,
      narrows: typeof body.narrows === 'string' ? body.narrows : null,
    };
  }

  // 2. An array: of scalars, or of records carrying an id and something to call it.
  if (Array.isArray(body)) {
    const values = body.map((item) => {
      if (item === null || item === undefined) return null;
      if (typeof item !== 'object') return choice(item, null);
      const value = item.id ?? item.value ?? item.uri ?? item.name;
      return value === undefined ? null : choice(value, item.title ?? item.name);
    }).filter(Boolean);
    return { values };
  }

  // 3. An object keyed by the identifier — a map of id to record, which is how a server
  //    that never thought about clients tends to publish a set.
  if (body && typeof body === 'object') {
    const entries = Object.entries(body);
    return { values: entries.map(([key, value]) => choice(key, value?.name ?? value?.title)) };
  }

  return { values: [], error: 'This listing is a single JSON scalar, not a set of values.' };
}

/** One value, and the label to show for it when the label says something the value does not. */
function choice(value, label) {
  const text = String(value);
  return { value: text, label: label != null && String(label) !== text ? String(label) : null };
}

/**
 * Open one vocabulary, and whatever cascades from it.
 *
 * The read happens in `refreshVariables`, not here: this only says that somebody wants it.
 */
function openVocabulary(uri) {
  if (!opened.includes(uri)) opened.push(uri);
  varsNote(null);
  refreshVariables();
}

/**
 * Put one back in the menu, and forget what was picked in it.
 *
 * The picks go because they are a claim about what you meant, and a claim made in a group
 * that is no longer on screen is one nothing can show you or take back. The bodies stay:
 * those are a cache, and re-opening should not re-read a live backend.
 */
function closeVocabulary(uri) {
  opened = opened.filter((held) => held !== uri);
  // The picks go with it, this one's and the whole chain's: the next resolve buries every
  // key that is no longer on screen, and re-rendering is what runs it.
  renderVariables();
  applyPicks();
}

/** The menu this column starts as: every vocabulary not already open. */
function vocabularyPicker(available) {
  const closed = available.filter((pair) => !opened.includes(pair.uri));
  const select = el('select', { class: 'vocab-add' });
  select.append(el('option', {
    value: '',
    text: closed.length
      ? (opened.length ? 'Add a parameter…' : 'Choose a parameter to start…')
      : 'Every parameter is open',
    disabled: !closed.length,
  }));
  for (const pair of closed) {
    select.append(el('option', { value: pair.uri, text: `${pair.variable} — ${pair.listing}` }));
  }
  select.disabled = !closed.length;
  select.addEventListener('change', () => {
    const uri = select.value;
    // Back to the placeholder: the select is a verb here, not a statement of what is showing.
    select.value = '';
    if (uri) openVocabulary(uri);
  });
  return el('div', { class: 'vocab-picker' }, [
    el('label', { class: 'vocab-picker-label', text: 'Parameter' }),
    select,
  ]);
}

function renderVariables() {
  const host = $('variables');
  host.replaceChildren();

  if (!state.selected) {
    host.append(el('p', { class: 'empty', text: 'Select a server in the header.' }));
    return;
  }
  const available = vocabularyPairs();
  if (!available.length) {
    host.append(el('p', {
      class: 'empty',
      text: `${state.selected} publishes no listing that pairs with a template, so there are `
        + 'no values to offer.',
    }));
    return;
  }

  host.append(vocabularyPicker(available));
  const groups = vocabularyGroups();
  if (!groups.length) {
    host.append(el('p', {
      class: 'empty',
      text: 'Nothing open yet. Choose a parameter above and its values — and whatever they '
        + 'narrow — appear here.',
    }));
    return;
  }

  for (const group of groups) host.append(vocabularyGroup(group));
  updateSendLabel();
}

function vocabularyGroup(pair) {
  const read = vocabularies.get(pair.uri) || { loading: true };
  // A listing that names its own template overrides the pairing found by prefix: the
  // server knows where its values are spent better than the URIs do. A `narrows` says the
  // same thing about a listing rather than a member, and the group is named for the
  // variable the chain has not bound yet either way.
  const spends = read.narrows || read.readOne || pair.template;
  const bound = pair.context || {};
  // Settled by `vocabularyGroups`, which had to know it before this group's own child could
  // be expanded. Reading it off the body again here would be a second answer to one question.
  const variable = pair.variable;
  const depth = pair.depth || 0;

  const head = el('div', { class: 'vocab-head' }, [
    el('span', { class: 'vocab-name', text: variable || '…' }),
    el('span', { class: 'vocab-from', text: pair.listing || pair.template }),
  ]);
  // Only the root wears it: the groups under it are not separately closeable, because they
  // are not separately opened -- they are what this one narrowed to.
  if (!depth) {
    const close = el('button', {
      type: 'button',
      class: 'vocab-close',
      text: '×',
      title: `Put ${variable || 'this parameter'} back in the menu`,
      'aria-label': `Close ${variable || 'this parameter'}`,
    });
    close.addEventListener('click', () => closeVocabulary(pair.uri));
    head.append(close);
  }

  const group = el('div', { class: depth ? 'vocab vocab-child' : 'vocab' }, [
    head,
    el('p', { class: 'vocab-template', text: `${read.narrows ? 'narrows' : 'spent on'} ${spends}` }),
  ]);
  // Why this group holds these values and not others. Without it a countries group under a
  // continents group is just a shorter list than the one you saw a moment ago.
  const context = Object.entries(bound);
  if (context.length) {
    group.append(el('p', {
      class: 'vocab-context',
      text: context.map(([name, value]) => `${name} = ${value}`).join(' · '),
    }));
  }

  // Drawn, but neither read nor given a pick: the group is here to say that picking above
  // is what fills it, which is a different thing from a listing that came back empty.
  if (pair.pending) {
    group.classList.add('vocab-pending');
    group.append(el('p', {
      class: 'note',
      text: pair.pending === 'pick'
        ? `Pick one ${pair.from} above to narrow this.`
        : `This listing needs ${pair.from}, which nothing above it publishes.`,
    }));
    return group;
  }

  if (read.loading) {
    group.append(el('p', { class: 'note', text: 'Reading…' }));
    return group;
  }
  if (read.error) {
    group.append(el('p', { class: 'vocab-error', text: read.error }));
    if (pair.listing) group.append(el('p', { class: 'vocab-from', text: pair.listing }));
    return group;
  }
  if (!read.values.length) {
    group.append(el('p', { class: 'note', text: 'This listing is empty.' }));
    return group;
  }

  const pick = pickFor(pair.uri, variable);

  // One or many, and the switch is the whole feature: one value fills the field, several
  // fill it in turn and send the form once per value.
  //
  // Except where a value buys another *listing*. `many` means "send the open form once per
  // value", and reading a listing sends no form; merging two continents' countries would
  // invent a vocabulary the server never published, with nothing on the chip to say which
  // continent each country came from. So a narrowing group picks one, and the ambiguity
  // never arises rather than being papered over.
  if (read.narrows) {
    pick.multi = false;
  } else {
    const modes = el('div', { class: 'vocab-modes', role: 'group' }, [
      modeButton(pick, false, 'one'),
      modeButton(pick, true, 'many'),
    ]);
    // Before the close button, which stays the last thing in the row: the control that
    // removes the group should not move when the group grows a switch.
    const headRow = group.querySelector('.vocab-head');
    headRow.insertBefore(modes, headRow.querySelector('.vocab-close'));
  }

  // Radios in `one`, boxes in `many`, and the same chip around either: the switch changes
  // what picking means, and the control under your cursor says which it currently is.
  const values = el('div', { class: 'vocab-values' });
  for (const item of read.values) {
    values.append(valueChoice(pick, pair.uri, variable, item, !!read.narrows));
  }
  group.append(values);

  const count = el('span', { class: 'vocab-count' });
  const clear = el('button', { type: 'button', class: 'ghost', text: 'Clear' });
  clear.addEventListener('click', () => {
    pick.values.clear();
    fillPick(pick, { quiet: true });
    // Clearing a narrowing group unmakes what it narrowed: the groups below go back to
    // pending, and their picks go with them. That is the resolver's job, not a re-render's.
    if (read.narrows) refreshVariables();
    else renderVariables();
  });
  group.append(el('div', { class: 'vocab-foot' }, [count, clear]));

  const say = () => {
    const n = pick.values.size;
    if (!n) count.textContent = 'Nothing picked.';
    else if (read.narrows) count.textContent = `Narrowed to ${[...pick.values][0]}.`;
    else if (!pick.multi) count.textContent = `${[...pick.values][0]} is in the field.`;
    else count.textContent = `${n} picked — the form is sent ${n} time${n === 1 ? '' : 's'}.`;
    clear.disabled = !n;
  };
  pick.say = say;
  say();
  return group;
}

/**
 * Take a buried pick's value back out of the form.
 *
 * The other half of "a pick lives as long as its group is on screen". Change the continent
 * and the country picked under the old one stops existing — but the form is still holding
 * it, and a template expanding to `.../africa/countries/nepal/animals` is a read that will
 * miss. The value has to go where the pick went.
 *
 * **Only what this pick put there.** A value you typed over it is yours, and a pick dying
 * elsewhere in the column is no reason to take it away. Strictly bound, like every other
 * write: a field carrying the variable's name, or nothing.
 */
function buryField(pick) {
  const control = controlFor(pick.variable, { strict: true });
  if (!control || control.value !== pickDisplay([...pick.values])) return;
  control.value = '';
  // As a keystroke would, so the expansion line and the problems recompute — the whole
  // point is that the form stops claiming a value it no longer has.
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.dispatchEvent(new Event('change', { bubbles: true }));
  markFanned(control, false);
}

/** The pick state for one vocabulary, created on first sight. */
function pickFor(uri, variable) {
  let pick = picks.get(uri);
  if (!pick) {
    pick = { variable, multi: false, values: new Set(), say: () => {} };
    picks.set(uri, pick);
  }
  pick.variable = variable;   // a re-read may have moved the listing to another template
  return pick;
}

function modeButton(pick, multi, label) {
  const button = el('button', {
    type: 'button',
    class: `vocab-mode${pick.multi === multi ? ' on' : ''}`,
    text: label,
    'aria-pressed': String(pick.multi === multi),
    title: multi
      ? 'Pick several; the form is sent once per value'
      : 'Pick one; it fills the field',
  });
  button.addEventListener('click', () => {
    if (pick.multi === multi) return;
    pick.multi = multi;
    // Narrowing keeps the first pick rather than dropping the lot: `many` -> `one` after
    // picking three is a change of mind about the fan-out, not about the animals.
    if (!multi) {
      const first = [...pick.values][0];
      pick.values = new Set(first === undefined ? [] : [first]);
    }
    fillPick(pick, { quiet: true });
    renderVariables();
  });
  return button;
}

/**
 * One value, as the control the current mode calls for.
 *
 * A radio in `one` and a box in `many`, both inside the same chip. The pair is the point:
 * picking looks like picking either way, and the shape of the control is what says whether
 * this vocabulary spends one value or several.
 */
function valueChoice(pick, group, variable, item, narrows) {
  const chosen = pick.values.has(item.value);
  const box = el('input', {
    type: pick.multi ? 'checkbox' : 'radio',
    // Radios need a shared name to be one group, and the listing's URI is the one name a
    // vocabulary already has that no other vocabulary shares.
    name: `vocab:${group}`,
    checked: chosen,
  });
  const wrap = el('label', {
    class: `vocab-value${chosen ? ' on' : ''}`,
    title: narrows
      ? `Narrow what follows to ${item.value}`
      : (pick.multi
        ? `Send the form once with ${item.value}`
        : `Put ${item.value} in ${variable}`),
  }, [
    box,
    el('span', { text: item.label || item.value }),
    item.label ? el('code', { text: item.value }) : null,
  ]);

  box.addEventListener('change', () => {
    if (pick.multi) {
      if (box.checked) pick.values.add(item.value);
      else pick.values.delete(item.value);
    } else {
      // The browser has already unchecked the other radio; this is the same fact in the
      // pick.
      pick.values = new Set(box.checked ? [item.value] : []);
    }
    for (const chip of wrap.parentElement.children) {
      chip.classList.toggle('on', chip.querySelector('input').checked);
    }
    pick.say();
    // `many` writes the whole set, and only where the set can be fanned out from. `one`
    // writes the value, and may fall back to the field you were last typing in.
    if (pick.multi) fillPick(pick);
    else if (box.checked) fillField(variable, item.value);
    updateSendLabel();
    // What this value bought is another listing, and which one depends on the value — so
    // the group below has to be resolved again and read. The read is keyed by the expanded
    // URI, so coming back to a continent you already opened costs nothing.
    if (narrows) refreshVariables();
  });
  return wrap;
}

/** How a set reads in a field it does not fit in: `[axolotl, capybara]`. */
function pickDisplay(values) {
  return values.length > 1 ? `[${values.join(', ')}]` : (values[0] ?? '');
}

/**
 * Show a whole pick in the field it binds to.
 *
 * One value goes in as itself. Several go in as `[a, b]` — not a value the form will ever
 * send, and not pretending to be one: it is the fan-out, written where the fan-out will
 * happen, so the form shows what the send button's `×6` is counting. Every reader of the
 * form knows to ask what it means: the template expands one line per value, the wire
 * preview shows the first call, and the send writes the real values in one at a time.
 *
 * Strictly bound, like the fan-out itself. A set written into a field that merely had focus
 * is a set that would be sent literally, since the fan-out would not rewrite it.
 */
function fillPick(pick, { quiet = false } = {}) {
  const values = [...pick.values];
  const say = quiet ? () => {} : varsNote;
  const control = controlFor(pick.variable, { strict: true });
  //: Nothing in the open form takes this variable -- or nothing is open yet. That is an
  //: ordinary way to work, not a mistake to report: you pick the values you want and then
  //: open the thing to spend them on. The group's own foot line already says how many are
  //: picked, and the send button counts them the moment a form that takes them is open.
  if (!control) {
    say(null);
    return;
  }
  // A select takes one of its own options, and a number input silently drops text it
  // cannot parse. Neither can hold a set, so both show the value the first call will use.
  const oneOnly = control.tagName === 'SELECT' || PARSED_INPUTS.has(control.type);
  say(putValue(control, oneOnly ? (values[0] ?? '') : pickDisplay(values)));
  markFanned(control, !oneOnly && values.length > 1);
}

/**
 * The picks that are still on screen, as `[key, pick]`.
 *
 * `vocabularyGroups` already drops the rest, but it only runs when the column resolves, and
 * the fan-out reads `picks` directly — so this is what keeps a pick made a moment before a
 * group closed out of a send that happens a moment after.
 */
function livePicks() {
  return [...picks.entries()].filter(([key]) => liveKeys.has(key));
}

/**
 * Write every pick into the open form. The form is new, or the values moved.
 *
 * **Every pick, not only the fanned-out ones.** This used to write `many` picks alone, on
 * the assumption that a single value had already gone into the form at the moment it was
 * clicked — true when the form was open first and the value picked second.
 *
 * A cascade reverses that order. You cannot pick a country until you have picked its
 * continent, so by the time the template that takes them is open, all three picks are
 * already made and clicking them again is exactly what nobody should have to do. The whole
 * chain is written in here instead.
 *
 * Safe because `fillPick` binds strictly: a value goes in a field that carries its name, or
 * it goes nowhere. Opening a form can therefore never scatter picks into whatever fields it
 * happened to have.
 */
function applyPicks() {
  for (const [, pick] of livePicks()) {
    if (pick.values.size) fillPick(pick, { quiet: true });
  }
  updateSendLabel();
}

/** The open form's fields that hold a set, as `fieldName -> values`. */
function fannedFields() {
  const fanned = new Map();
  for (const { control, values } of boundPicks()) {
    if (values.length < 2) continue;
    const field = control.closest('[data-field]');
    if (field) fanned.set(field.dataset.field, values);
  }
  return fanned;
}

/** `{id: '[a, b]'}` and `{id: ['a','b']}` -> `[{id: 'a'}, {id: 'b'}]`. */
function spread(values, fanned) {
  let combos = [values];
  for (const [name, picked] of fanned) {
    if (!(name in values)) continue;
    // The picked value is always a string; the collected one says what the field's schema
    // made of it, and the preview should not turn a number into a quoted one.
    const like = (value) => (typeof values[name] === 'number' ? Number(value) : value);
    combos = combos.flatMap((combo) => picked.map((value) => ({ ...combo, [name]: like(value) })));
  }
  return combos;
}

/** The field wears the fact that it is holding a set rather than a value. */
function markFanned(control, fanned) {
  control.closest('.field')?.classList.toggle('field-fanned', fanned);
}

// ── Fanning a form out over several values ─────────────────────────────────────
//
// Nothing here reaches inside a form. Each combination is written into the controls as an
// `input` event and the form is then collected and sent exactly as a click would collect
// and send it — so a fanned-out call is byte-identical to the one you would have made by
// typing the value yourself, which is the same reason the middle column speaks MCP.

/** The picks that name a field in the open form, as `[{variable, control, values}]`. */
function boundPicks() {
  const bound = [];
  for (const [, pick] of livePicks()) {
    if (!pick.multi || !pick.values.size) continue;
    const control = controlFor(pick.variable, { strict: true });
    if (control) bound.push({ variable: pick.variable, control, values: [...pick.values] });
  }
  return bound;
}

/** Every combination of the bound picks, as `[[{control, value}, …], …]`. */
function combinations() {
  let combos = [[]];
  for (const { control, values } of boundPicks()) {
    combos = combos.flatMap((combo) => values.map((value) => [...combo, { control, value }]));
  }
  return combos.length === 1 && !combos[0].length ? [] : combos;
}

/** Run `once` for each combination, or exactly once when nothing is fanned out. */
async function fanOut(once) {
  const combos = combinations();
  if (!combos.length) return once();
  if (combos.length > FAN_OUT_ASKS_ABOVE
      && !confirm(`This sends the form ${combos.length} times. Go ahead?`)) return;
  for (const combo of combos) {
    for (const { control, value } of combo) putValue(control, value);
    await once();                 // sequential: the result cards land in the order picked
  }
  // The form is left holding the last combination otherwise, which reads as though the
  // picks had collapsed to whatever went out last.
  applyPicks();
}

/** Name the send button so a fan-out can say how many calls it is. */
function registerSend(button, base) {
  sendControl = { button, base };
  updateSendLabel();
}

function setSendBase(base) {
  if (sendControl) sendControl.base = base;
  updateSendLabel();
}

function updateSendLabel() {
  if (!sendControl || !$('detail').contains(sendControl.button)) return;
  const n = combinations().length;
  sendControl.button.textContent = n > 1 ? `${sendControl.base} ×${n}` : sendControl.base;
}

// ── Putting a value in a form ──────────────────────────────────────────────────

/**
 * The control in the open form that `name` belongs in, or null.
 *
 * The form may be a tool's, a prompt's or a template's — they all tag their fields with
 * `data-field`, so one lookup covers the three. The loosening below stops at the point
 * where a wrong guess would be worse than none: an exact name, then the same name spelt in
 * another case or with other separators, then a field whose name ends in it (`animalId` for
 * `id`), then whatever you were last typing in.
 */
function controlFor(name, { strict = false } = {}) {
  const detail = $('detail');
  const fields = [...detail.querySelectorAll('[data-field]')];
  const norm = (text) => String(text).toLowerCase().replace(/[^a-z0-9]/g, '');
  const target = fields.find((f) => f.dataset.field === name)
    || fields.find((f) => norm(f.dataset.field) === norm(name))
    || fields.find((f) => norm(f.dataset.field).endsWith(norm(name)) && norm(name).length > 1);

  // `strict` drops the last-focused fallback. Filling a field you were just typing in is a
  // helpful guess; *fanning a form out* into it because a box is ticked somewhere is not.
  const fallback = !strict && detail.contains(lastFocused) ? lastFocused : null;
  return target?.querySelector('input:not([readonly]), select, textarea') || fallback;
}

/** Put `value` in `control`, the way a keystroke would. Returns why not, or null. */
function putValue(control, value) {
  if (control.tagName === 'SELECT') {
    // Matched on either spelling. A boolean or a `null` select wears its value directly; a
    // schema `enum` wears the choice's *index*, because `{"enum": [0, 1, 3, 5]}` has to
    // come back as the number 3 rather than the string "3" -- so `schema_form.js` carries
    // the choice's own spelling on `dataset.value` for exactly this comparison. Matching
    // only `option.value` rejected every enum field, about values it plainly offered.
    const option = [...control.options]
      .find((candidate) => candidate.value === value || candidate.dataset.value === value);
    if (!option) {
      return `${value} is not one of the choices this field offers.`;
    }
    control.value = option.value;
  } else if (control.type === 'checkbox') {
    return 'That field is a checkbox, so a value cannot be put in it.';
  } else {
    control.value = value;
  }

  // The forms recompute their preview and their problems off `input`, so the value has to
  // arrive the way a keystroke would rather than by assignment alone.
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.dispatchEvent(new Event('change', { bubbles: true }));

  const field = control.closest('.field') || control;
  // Whatever this field was holding, it is holding a plain value now. `fillPick` puts the
  // mark back when what it just wrote is a set.
  field.classList.remove('field-fanned');
  field.classList.remove('field-filled');
  void field.offsetWidth;               // restart the animation on a second pick
  field.classList.add('field-filled');
  field.scrollIntoView({ block: 'nearest' });
  return null;
}

/** Put `value` in the open form's `name` field, if the open form has one. */
function fillField(name, value) {
  const control = controlFor(name);
  //: Nowhere to put it, which is not news -- the same reasoning as `fillPick`. Picking a
  //: value before opening the thing that takes it is how the column is meant to be used,
  //: and a line of complaint under the heading every time you do it is the column talking
  //: over the work. What `putValue` reports below is different: those are cases where there
  //: *is* a field and the value cannot go in it, which is worth a word.
  if (!control) {
    varsNote(null);
    return;
  }
  varsNote(putValue(control, value));
  updateSendLabel();
}

/** A line under the column head, or nothing. The only place this column talks back. */
function varsNote(text) {
  const line = $('vars-note');
  line.textContent = text || '';
  line.hidden = !text;
}

$('btn-vars-refresh').addEventListener('click', () => {
  vocabularies.clear();
  varsNote(null);
  refreshVariables();
});

// ── Suggestions for one argument ───────────────────────────────────────────────
//
// `completion/complete` is the protocol's own answer to the question the variables column
// answers by hand: what may go in this box? The two are worth having side by side. The
// column is how a *person* browses a vocabulary — every value visible, several pickable,
// the fan-out counted on the button. This is how a box gets filled while you are typing in
// it, which is the thing a model does and a person does more often.
//
// It is a `<datalist>` rather than a `<select>` on purpose. `putValue` refuses a value that
// is not among a select's options, so a constraining control here would break the column's
// own fill — and the `[a, b]` a fan-out writes into a field is not a value any server would
// ever suggest. A datalist offers without constraining, which is what a *suggestion* is.

//: How long after a keystroke the suggestion is asked for. Long enough that typing a word
//: is one request rather than five, short enough not to arrive after you have stopped.
const COMPLETE_DEBOUNCE_MS = 180;

//: Every completion request in order, so a slow answer cannot overwrite a newer one. A
//: WebSocket has no `AbortController`, so the sequence number is the whole mechanism.
let completionSeq = 0;

//: Ids for the datalists, counted apart from the requests: sharing one counter would let
//: opening a form cancel a request that was already in flight for a different field.
let completionLists = 0;

function completionsEnabled() {
  return !!(state.mcp?.capabilities || {}).completions;
}

/**
 * Offer server-suggested values in `input`, for the argument `name` of `ref`.
 *
 * `siblings()` is what makes this a cascade rather than a list: it returns the arguments of
 * the same form that are already filled in, and the server is free to narrow by them — the
 * countries of the continent above, rather than every country there is.
 *
 * Nothing here can fail loudly. A gateway that does not know the method, a backend that is
 * down, a request that times out: all of them clear the list and say so in the tooltip. A
 * suggestion that broke the form it was helping with would be worse than no suggestion.
 */
function attachCompletions(input, { ref, name, siblings }) {
  // Called after the input is in its field, because a `<datalist>` has to be *somewhere* in
  // the document for the browser to find it by id, and an input that is not yet in one has
  // nowhere to put it.
  if (!completionsEnabled() || !input.parentElement) return;
  const list = el('datalist', { id: `completions-${(completionLists += 1)}` });
  input.setAttribute('list', list.id);
  input.setAttribute('autocomplete', 'off');
  input.parentElement.append(list);

  let timer = null;
  const ask = async () => {
    const value = input.value;
    // A field holding a fan-out holds `[a, b]`, which is not a prefix of anything.
    if (value.startsWith('[')) return;
    const seq = (completionSeq += 1);
    try {
      const result = await state.mcp.request('completion/complete', {
        ref,
        argument: { name, value },
        context: { arguments: siblings() },
      });
      if (seq !== completionSeq) return;      // a later keystroke already asked
      const values = result?.completion?.values || [];
      list.replaceChildren(...values.map((v) => el('option', { value: String(v) })));
      const more = result?.completion?.hasMore;
      input.title = more ? `${values.length} of ${result.completion.total} suggestions` : '';
    } catch (err) {
      if (seq !== completionSeq) return;
      list.replaceChildren();
      input.title = `No suggestions: ${err.message || err}`;
    }
  };
  const soon = () => {
    clearTimeout(timer);
    timer = setTimeout(ask, COMPLETE_DEBOUNCE_MS);
  };
  // On focus as well as on input, because the useful moment is the one before anything has
  // been typed: an empty box is where a person most wants to be told what goes in it.
  input.addEventListener('focus', soon);
  input.addEventListener('input', soon);
}

/** Everything filled in beside `name`, as `context.arguments` wants it. */
function siblingValues(values, name) {
  const out = {};
  for (const [key, value] of Object.entries(values)) {
    if (key === name || value === '' || value === undefined || value === null) continue;
    // A field holding a fan-out is holding several values; none of them is *the* one that
    // narrows this, so it says nothing here rather than the wrong thing.
    if (typeof value === 'string' && value.startsWith('[')) continue;
    out[key] = String(value);
  }
  return out;
}

// ── The detail panel: a form, and the button that sends it ─────────────────────

function openItem(kind, entry) {
  state.item = { kind, entry };
  renderPrimitives();
  const detail = $('detail');
  detail.replaceChildren();
  // The old form's send button has just left the page; whatever replaces it registers
  // itself below.
  sendControl = null;

  const head = el('div', { class: 'detail-head' }, [
    el('h3', { text: localName(kind, entry) }),
    el('code', { class: 'detail-id', text: itemId(entry) }),
  ]);
  detail.append(head);
  if (entry.description) detail.append(el('p', { class: 'detail-desc', text: entry.description }));

  // The form is built first and filled second: the picks are written into whatever fields
  // it turns out to have, exactly as they would be if you had picked them now.
  if (kind === 'tools') renderToolDetail(detail, entry);
  else if (kind === 'prompts') renderPromptDetail(detail, entry);
  else renderResourceDetail(detail, entry, kind === 'templates');
  applyPicks();
}

function renderToolDetail(detail, entry) {
  const preview = el('pre', { class: 'block preview' }, [el('code', { text: '{}' })]);
  const problems = el('ul', { class: 'problems', hidden: true });

  const form = buildForm(entry.inputSchema, { onChange: update });
  detail.append(form.element);

  if (entry.outputSchema) {
    detail.append(el('details', { class: 'raw' }, [
      el('summary', { text: 'outputSchema' }),
      el('pre', { class: 'block' }, [el('code', { text: pretty(entry.outputSchema) })]),
    ]));
  }

  const send = el('button', { type: 'button', class: 'primary', text: 'Call tool' });
  const bar = el('div', { class: 'detail-actions' }, [
    send,
    el('details', { class: 'raw preview-wrap' }, [el('summary', { text: 'Wire payload' }), preview]),
  ]);
  detail.append(problems, bar);

  function update() {
    try {
      // The first of the calls, when a field is holding a set: the payload of a fan-out is
      // n payloads, and the first one is the only honest single thing to show.
      const [args] = spread(form.collect(), fannedFields());
      preview.firstChild.textContent = pretty({
        method: 'tools/call', params: { name: entry.name, arguments: args },
      });
      const found = form.problems();
      problems.replaceChildren(...found.map((text) => el('li', { text })));
      problems.hidden = found.length === 0;
      // Advice, not a gate. The button stays live: the gateway and the backend are the
      // authority on what is acceptable, and a form that refused to send would be
      // pretending to be one.
      setSendBase(found.length ? 'Call anyway' : 'Call tool');
    } catch (err) {
      preview.firstChild.textContent = String(err.message);
      problems.replaceChildren(el('li', { text: err.message }));
      problems.hidden = false;
      setSendBase('Call tool');
    }
  }
  registerSend(send, 'Call tool');
  update();

  // Collected inside the loop, not outside it: a fan-out writes each value into the form
  // and this reads the form back, so every call is the one the visible form describes.
  const once = async () => {
    let args;
    try {
      args = form.collect();
    } catch (err) {
      problems.replaceChildren(el('li', { text: err.message }));
      problems.hidden = false;
      return;
    }
    await invoke({
      title: entry.name,
      subtitle: 'tools/call',
      method: 'tools/call',
      params: { name: entry.name, arguments: args },
      render: renderToolResult,
      // A tool-level failure arrives as a *successful* result carrying isError, which is
      // MCP's contract. The card is marked failed so it reads as one, without pretending
      // the JSON-RPC call failed.
      failedIf: (result) => !!result.isError,
    }, send);
  };
  send.addEventListener('click', () => fanOut(once));
}

function renderPromptDetail(detail, entry) {
  const form = buildPromptForm(entry.arguments, { onChange: () => {} });
  detail.append(form.element);
  // The namespaced name, which the gateway splits: a prompt ref is routed exactly the way
  // `prompts/get` is. `collect()` already drops the empty fields, so what is left is what
  // has actually been decided — the cascade's context, without having to say so.
  for (const { name, input } of form.fields || []) {
    attachCompletions(input, {
      ref: { type: 'ref/prompt', name: entry.name },
      name,
      siblings: () => siblingValues(form.collect(), name),
    });
  }
  const send = el('button', { type: 'button', class: 'primary', text: 'Get prompt' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  registerSend(send, 'Get prompt');
  send.addEventListener('click', () => fanOut(() => invoke({
    title: entry.name,
    subtitle: 'prompts/get',
    method: 'prompts/get',
    // Read per call, so a fan-out sends the arguments it just wrote rather than the first
    // set it collected.
    params: { name: entry.name, arguments: form.collect() },
    render: renderPromptResult,
  }, send)));
}

function renderResourceDetail(detail, entry, isTemplate) {
  const meta = [entry.mimeType, entry.size ? `${entry.size} B` : null].filter(Boolean).join(' · ');
  if (meta) detail.append(el('p', { class: 'detail-desc', text: meta }));

  let uriOf = () => entry.uri;

  if (isTemplate) {
    const names = templateVariables(entry.uriTemplate);
    const inputs = new Map();
    const wrap = el('div', { class: 'fields' });
    const resolved = el('code', { class: 'detail-id expansions' });
    const valuesNow = () => {
      const values = {};
      for (const [name, input] of inputs) values[name] = input.value;
      return values;
    };
    // A field holding `[a, b]` expands to a line per value rather than to one URI with a
    // bracket in it: what a fan-out is about to read is what this should show.
    const refresh = () => {
      const combos = spread(valuesNow(), fannedFields());
      const shown = combos.slice(0, EXPANSIONS_SHOWN)
        .map((values) => expandTemplate(entry.uriTemplate, values));
      if (combos.length > shown.length) shown.push(`…and ${combos.length - shown.length} more`);
      resolved.textContent = shown.join('\n');
    };
    for (const name of names) {
      const input = el('input', { type: 'text', spellcheck: false });
      input.addEventListener('input', refresh);
      inputs.set(name, input);
      wrap.append(el('div', { class: 'field field-string', dataset: { field: name } }, [
        el('label', { class: 'field-label' }, [el('span', { class: 'field-name', text: name })]),
        input,
      ]));
      // The gateway's own unexpanded spelling: `ref/resource` names the *template*, which
      // is exactly what `router.complete` decodes back into the backend's. Every other
      // field of this same template is the context, which is what makes `{continent}`
      // narrow `{country}` rather than the two being filled in independently.
      attachCompletions(input, {
        ref: { type: 'ref/resource', uri: entry.uriTemplate },
        name,
        siblings: () => siblingValues(valuesNow(), name),
      });
    }
    if (!names.length) wrap.append(el('p', { class: 'note', text: 'This template names no variables.' }));
    detail.append(wrap, el('p', { class: 'note' }, [el('span', { text: 'Expands to ' }), resolved]));
    refresh();
    // Expanded here rather than sent as a template: `resources/read` takes a URI, and the
    // expansion is the client's job in MCP exactly as it is in RFC 6570. Read off the
    // inputs rather than off the line above, which may be showing six of them.
    uriOf = () => expandTemplate(entry.uriTemplate, valuesNow());
  }

  const send = el('button', { type: 'button', class: 'primary', text: 'Read resource' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  registerSend(send, 'Read resource');
  // `uriOf` is read per call: a template fanned out over six ids expands six times.
  send.addEventListener('click', () => fanOut(() => invoke({
    title: localName(isTemplate ? 'templates' : 'resources', entry),
    subtitle: 'resources/read',
    method: 'resources/read',
    params: { uri: uriOf() },
    render: renderResourceResult,
  }, send)));
}

/** Send one MCP request and put the answer on the right. */
async function invoke({ title, subtitle, method, params, render, failedIf }, button) {
  const started = performance.now();
  if (button) button.disabled = true;
  try {
    const result = await state.mcp.request(method, params);
    pushCard({
      title,
      subtitle,
      request: { method, params },
      body: render(result),
      elapsedMs: performance.now() - started,
      raw: result,
      failed: failedIf ? failedIf(result) : false,
    });
  } catch (err) {
    const error = err instanceof RpcError
      ? { code: err.code, message: err.message, data: err.data }
      : { code: -32000, message: String(err.message || err), data: null };
    pushCard({
      title,
      subtitle,
      request: { method, params },
      body: renderError(error),
      elapsedMs: performance.now() - started,
      raw: { error },
      failed: true,
    });
  } finally {
    if (button) button.disabled = false;
  }
}

// ── The results column ─────────────────────────────────────────────────────────

function pushCard(options) {
  const results = $('results');
  const empty = results.querySelector('.empty');
  if (empty) empty.remove();
  results.prepend(resultCard(options));
}

$('btn-clear-results').addEventListener('click', () => {
  $('results').replaceChildren(el('p', {
    class: 'empty', text: 'Invoke a tool, get a prompt, or read a resource.',
  }));
});

$('btn-reload').addEventListener('click', () => admin('admin.reload', {}));
$('btn-refresh').addEventListener('click', async () => { await refreshAdmin(); await refreshListings(); });

// ── The server editor ──────────────────────────────────────────────────────────

//: The daemon's own timeouts, from `config.py`. Mirrored rather than fetched because
//: `admin.config.get` reports each spec with its defaults already folded in and never
//: reports the file's `defaults:` block, so there is nothing to read them from -- see
//: python-mcp-gateway-cw4. They are shown as placeholders only, so a drift here misleads
//: about an unset field and cannot write a wrong value.
const DEFAULT_TIMEOUT = 30;
const DEFAULT_STARTUP_TIMEOUT = 20;

// Three columns, so the whole spec is visible at once rather than scrolled past: what
// the daemon runs, what it runs it with, and how it treats the result. The `name` field
// only exists when adding, and is prepended to the first column there.
//
// The fourth entry is a placeholder -- what the field means when you leave it alone. For
// the two timeouts that is the daemon's own default, which `config.py` applies to any spec
// that omits the key; for the rest it is a worked example of the shape the field wants.
// Placeholders and not values, deliberately: a prefilled `30` is a `30` *written to
// servers.yaml*, which would quietly override the file's own `defaults:` block. Left blank
// the key is omitted (see `collectEditor`) and the fallback chain still runs.
const EDITOR_GROUPS = [
  ['Process', [
    ['command', 'text', 'The executable, e.g. npx or python', 'npx'],
    ['description', 'text', 'What this server is for', 'Read-only files under /srv'],
    ['args', 'lines', 'One argument per line', '-y\n@modelcontextprotocol/server-filesystem\n/srv'],
    ['cwd', 'text', 'Working directory (optional)', "the daemon's own"],
  ]],
  ['Environment', [
    ['env', 'pairs', 'KEY=${SECRET_NAME}, one per line. Values live in gateway.env, never here',
      'API_KEY=${FILES_API_KEY}'],
    ['env_passthrough', 'lines', 'Variables inherited from the daemon, one per line', 'PATH\nHOME'],
  ]],
  ['Behaviour', [
    ['timeout', 'number', 'Per-request seconds', String(DEFAULT_TIMEOUT)],
    ['startup_timeout', 'number', 'Spawn + initialize + first listing, in seconds',
      String(DEFAULT_STARTUP_TIMEOUT)],
    ['enabled', 'boolean', 'Spawn this backend'],
    ['required', 'boolean', 'A failure here is fatal to the daemon'],
  ]],
];
function openEditor(name) {
  const existing = name ? (state.config.servers?.[name] || {}) : {};
  const dialog = $('server-dialog');
  const form = $('server-form');
  form.replaceChildren();

  form.append(el('h2', { text: name ? `Edit ${name}` : 'Add a server' }));
  form.append(el('p', {
    class: 'note',
    text: 'This writes servers.yaml, which is committed. Credential values belong in '
      + 'gateway.env and are referenced here as ${NAME}; nothing in this dialog can read '
      + 'or write one.',
  }));

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
      column.append(field(key, editorInput(key, kind, existing[key], placeholder, inputs), help));
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

state.key = initialKey();

// In the shell, the host knows things the page cannot: that the gateway is still importing,
// that it exited, that it is on its fourth restart, and what it last said on stderr. Without
// this the first launch is a blank gate for as long as a cold interpreter takes to start,
// and a `servers.yaml` the daemon refuses is a window that says "connecting" forever with
// the reason sitting unread in the host's log buffer.
if (inShell) {
  onGatewayState((status) => {
    if (status.state === 'listening') return;      // the sockets speak for themselves
    const last = status.log.length ? status.log[status.log.length - 1] : '';
    showGate({
      idle: 'Starting the gateway…',
      starting: 'Starting the gateway…',
      restarting: `The gateway stopped; restarting (attempt ${status.attempt})…`,
      failed: `The gateway could not start. ${status.reason || last}`,
    }[status.state] || last);
  });
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
  EDITOR_GROUPS,
  openEditor,
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
  controlFor,
  putValue,
  fillField,
  openItem,
  gatewayUri,
};
