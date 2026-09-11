// The three columns, and everything that moves between them.
//
// Two sockets, deliberately. `/admin` answers what is configured and what is running;
// `/mcp` answers what those backends actually publish, through a real MCP handshake. The
// second half is not a convenience: a UI that asked `/admin` for a tool listing would be
// showing its own rendering of the catalogue, and the whole point of a test bench is that
// what you exercise here is byte-identical to what the model gets.

import { AdminSocket, McpSocket, RpcError } from './rpc.js';
import { buildForm, buildPromptForm, templateVariables, expandTemplate } from './schema_form.js';
import {
  renderToolResult, renderPromptResult, renderResourceResult, renderError, resultCard, pretty,
} from './render.js';

// Mirrors `naming.py`. A server name may contain neither, which is what makes a split on
// the first separator unambiguous — see naming.md.
const SEPARATOR = '__';
const RESOURCE_SCHEME = 'mcpgw';

//: The gateway's own meta-tools live under this name and have no backend behind them, so
//: they appear in the left column as a synthetic entry rather than going unreachable.
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
      if (socket.attempt >= RETRIES_BEFORE_GATE) {
        socket.close();
        $('gate-message').textContent += ' Retrying stopped; press Connect to try again.';
      }
    }
    updateMeta();
  });
}

function updateMeta() {
  const info = state.mcp?.serverInfo;
  const bits = [];
  if (info) bits.push(`${info.name || 'gateway'} ${info.version || ''}`.trim());
  if (state.status) bits.push(`up ${formatDuration(state.status.uptime_seconds)}`);
  $('bar-meta').textContent = bits.join(' · ');
}

const formatDuration = (seconds) => {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
};

// ── Left column ────────────────────────────────────────────────────────────────

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
  renderStatus();
  updateMeta();
}

function renderBackends() {
  const list = $('backends');
  list.replaceChildren();

  for (const backend of state.backends) {
    list.append(backendRow(backend));
  }

  // The gateway's own meta-tools have no backend behind them. Without this row they are
  // listed by `/mcp` and reachable from nowhere in the UI.
  const metaButton = el('button', { type: 'button', class: 'backend-main' }, [
    el('span', { class: 'dot dot-meta' }),
    el('span', { class: 'backend-name', text: ADMIN_PREFIX }),
    el('span', { class: 'backend-note', text: 'the gateway itself' }),
  ]);
  metaButton.addEventListener('click', () => select(ADMIN_PREFIX));
  list.append(el('li', {
    class: `backend backend-meta${state.selected === ADMIN_PREFIX ? ' on' : ''}`,
  }, [metaButton]));

  if (!state.backends.length) {
    list.prepend(el('li', { class: 'empty', text: 'No backends configured.' }));
  }
}

function backendRow(backend) {
  const missing = state.missing[backend.name] || [];
  const spec = state.config.servers?.[backend.name] || {};
  const row = el('li', {
    class: `backend${state.selected === backend.name ? ' on' : ''}`
      + (backend.enabled ? '' : ' off'),
  });

  const main = el('button', { type: 'button', class: 'backend-main' }, [
    el('span', {
      class: `dot dot-${backend.status}`,
      title: backend.error || backend.status,
    }),
    el('span', { class: 'backend-name', text: backend.name }),
    el('span', {
      class: 'backend-note',
      text: backend.description || spec.description || backend.command || '',
    }),
  ]);
  main.addEventListener('click', () => select(backend.name));
  row.append(main);

  if (backend.error) row.append(el('p', { class: 'backend-error', text: backend.error }));
  if (missing.length) {
    row.append(el('p', { class: 'backend-missing' }, [
      el('span', { text: 'Missing from gateway.env: ' }),
      el('code', { text: missing.join(', ') }),
    ]));
  }

  const actions = el('div', { class: 'backend-actions' });
  actions.append(action('Restart', () => admin('admin.backend.restart', { name: backend.name })));
  actions.append(action(backend.enabled ? 'Disable' : 'Enable', () => admin('admin.backend.update', {
    name: backend.name, changes: { enabled: !backend.enabled },
  })));
  actions.append(action('Edit', () => openEditor(backend.name)));
  actions.append(action('Remove', () => {
    if (!confirm(`Remove ${backend.name} from servers.yaml?`)) return null;
    return admin('admin.backend.remove', { name: backend.name });
  }, 'danger'));
  row.append(actions);
  return row;
}

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

function renderStatus() {
  const kv = $('status-kv');
  kv.replaceChildren();
  const status = state.status;
  if (!status) {
    kv.append(el('dt', { text: 'status' }), el('dd', { text: 'unavailable' }));
    return;
  }
  const rows = [
    ['version', status.version],
    ['bind', status.bind],
    ['uptime', formatDuration(status.uptime_seconds)],
    ['clients', String((status.connections || []).length)],
    ['config', status.config_path],
    ['env', status.env_path],
  ];
  for (const [k, v] of rows) {
    kv.append(el('dt', { text: k }), el('dd', { text: String(v ?? '') }));
  }
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
  for (const entry of logEntries) {
    if ((LEVELS[entry.level] || 0) < floor) continue;
    list.append(el('li', { class: `log-${(entry.level || '').toLowerCase()}` }, [
      el('span', { class: 'log-time', text: new Date((entry.time || 0) * 1000).toLocaleTimeString() }),
      el('span', { class: 'log-level', text: entry.level || '' }),
      el('span', { class: 'log-message', text: entry.message || '' }),
    ]));
  }
  list.scrollTop = list.scrollHeight;
}

$('log-level').addEventListener('change', renderLog);
$('btn-log-clear').addEventListener('click', () => { logEntries = []; renderLog(); });

// ── Middle column ──────────────────────────────────────────────────────────────

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
}

function renderPrimitives() {
  $('mid-title').textContent = state.selected || 'Primitives';
  for (const kind of Object.keys(state.listings)) {
    const count = state.selected
      ? (state.listings[kind] || []).filter((e) => ownerOf(kind, e) === state.selected).length
      : 0;
    document.querySelector(`[data-count="${kind}"]`).textContent = String(count);
  }

  const list = $('primitives');
  list.replaceChildren();
  if (!state.selected) {
    list.append(el('li', { class: 'empty', text: 'Select a backend on the left.' }));
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

// ── The detail panel: a form, and the button that sends it ─────────────────────

function openItem(kind, entry) {
  state.item = { kind, entry };
  renderPrimitives();
  const detail = $('detail');
  detail.replaceChildren();

  const head = el('div', { class: 'detail-head' }, [
    el('h3', { text: localName(kind, entry) }),
    el('code', { class: 'detail-id', text: itemId(entry) }),
  ]);
  detail.append(head);
  if (entry.description) detail.append(el('p', { class: 'detail-desc', text: entry.description }));

  if (kind === 'tools') return renderToolDetail(detail, entry);
  if (kind === 'prompts') return renderPromptDetail(detail, entry);
  return renderResourceDetail(detail, entry, kind === 'templates');
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
      const args = form.collect();
      preview.firstChild.textContent = pretty({
        method: 'tools/call', params: { name: entry.name, arguments: args },
      });
      const found = form.problems();
      problems.replaceChildren(...found.map((text) => el('li', { text })));
      problems.hidden = found.length === 0;
      // Advice, not a gate. The button stays live: the gateway and the backend are the
      // authority on what is acceptable, and a form that refused to send would be
      // pretending to be one.
      send.textContent = found.length ? 'Call anyway' : 'Call tool';
    } catch (err) {
      preview.firstChild.textContent = String(err.message);
      problems.replaceChildren(el('li', { text: err.message }));
      problems.hidden = false;
      send.textContent = 'Call tool';
    }
  }
  update();

  send.addEventListener('click', async () => {
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
  });
}

function renderPromptDetail(detail, entry) {
  const form = buildPromptForm(entry.arguments, { onChange: () => {} });
  detail.append(form.element);
  const send = el('button', { type: 'button', class: 'primary', text: 'Get prompt' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  send.addEventListener('click', () => invoke({
    title: entry.name,
    subtitle: 'prompts/get',
    method: 'prompts/get',
    params: { name: entry.name, arguments: form.collect() },
    render: renderPromptResult,
  }, send));
}

function renderResourceDetail(detail, entry, isTemplate) {
  const meta = [entry.mimeType, entry.size ? `${entry.size} B` : null].filter(Boolean).join(' · ');
  if (meta) detail.append(el('p', { class: 'detail-desc', text: meta }));

  let uriOf = () => entry.uri;

  if (isTemplate) {
    const names = templateVariables(entry.uriTemplate);
    const inputs = new Map();
    const wrap = el('div', { class: 'fields' });
    const resolved = el('code', { class: 'detail-id' });
    const refresh = () => {
      const values = {};
      for (const [name, input] of inputs) values[name] = input.value;
      resolved.textContent = expandTemplate(entry.uriTemplate, values);
    };
    for (const name of names) {
      const input = el('input', { type: 'text', spellcheck: false });
      input.addEventListener('input', refresh);
      inputs.set(name, input);
      wrap.append(el('div', { class: 'field field-string' }, [
        el('label', { class: 'field-label' }, [el('span', { class: 'field-name', text: name })]),
        input,
      ]));
    }
    if (!names.length) wrap.append(el('p', { class: 'note', text: 'This template names no variables.' }));
    detail.append(wrap, el('p', { class: 'note' }, [el('span', { text: 'Expands to ' }), resolved]));
    refresh();
    // Expanded here rather than sent as a template: `resources/read` takes a URI, and the
    // expansion is the client's job in MCP exactly as it is in RFC 6570.
    uriOf = () => resolved.textContent;
  }

  const send = el('button', { type: 'button', class: 'primary', text: 'Read resource' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  send.addEventListener('click', () => invoke({
    title: localName(isTemplate ? 'templates' : 'resources', entry),
    subtitle: 'resources/read',
    method: 'resources/read',
    params: { uri: uriOf() },
    render: renderResourceResult,
  }, send));
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

// ── Right column ───────────────────────────────────────────────────────────────

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

const EDITOR_FIELDS = [
  ['command', 'text', 'The executable, e.g. npx or python'],
  ['args', 'lines', 'One argument per line'],
  ['env', 'pairs', 'KEY=${SECRET_NAME}, one per line. Values live in gateway.env, never here'],
  ['env_passthrough', 'lines', 'Variables inherited from the daemon, one per line'],
  ['cwd', 'text', 'Working directory (optional)'],
  ['description', 'text', 'What this server is for'],
  ['timeout', 'number', 'Per-request seconds'],
  ['startup_timeout', 'number', 'Spawn + initialize + first listing, in seconds'],
  ['enabled', 'boolean', 'Spawn this backend'],
  ['required', 'boolean', 'A failure here is fatal to the daemon'],
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
  if (!name) {
    const input = el('input', { type: 'text', required: true, spellcheck: false });
    inputs.set('name', { kind: 'text', input });
    form.append(field('name', input, 'The namespace its tools appear under. No "__", "/" or ":".'));
  }

  for (const [key, kind, help] of EDITOR_FIELDS) {
    let input;
    const value = existing[key];
    if (kind === 'lines') {
      input = el('textarea', { rows: 3, spellcheck: false, value: (value || []).join('\n') });
    } else if (kind === 'pairs') {
      const pairs = Object.entries(value || {}).map(([k, v]) => `${k}=${v}`);
      input = el('textarea', { rows: 3, spellcheck: false, value: pairs.join('\n') });
    } else if (kind === 'boolean') {
      input = el('input', { type: 'checkbox', checked: value ?? (key === 'enabled') });
    } else if (kind === 'number') {
      input = el('input', { type: 'number', step: '0.5', min: '0', value: value ?? '' });
    } else {
      input = el('input', { type: 'text', spellcheck: false, value: value ?? '' });
    }
    inputs.set(key, { kind, input });
    form.append(field(key, input, help));
  }

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

function field(key, input, help) {
  return el('div', { class: 'field' }, [
    el('label', { class: 'field-label' }, [el('span', { class: 'field-name', text: key })]),
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

// ── Go ─────────────────────────────────────────────────────────────────────────

state.key = initialKey();
connect();
