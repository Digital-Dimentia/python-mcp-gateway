// The primitives column: what the selected server publishes, and the row you click.
//
// The left column of the Basics screen. Four kinds behind four tabs, each with a live count,
// filtered by one search box, and every row a button that opens the detail panel on it. The
// row is deliberately austere -- name, one line of description, and the annotation badges
// that matter before you press anything -- because a column you read down at forty entries
// cannot also be a column that explains each one. The rest is in the panel, and on hover.
//
// ## What this column is handed
//
// `port`, installed by `app.js`:
//
//   state             the gateway state object; this column reads `listings`, `selected`,
//                     `kind` and `item`, and sets the last two.
//   openItem(k, e)    open the detail panel on a row. The panel is another module's, and
//                     what this one knows about it is that a click opens it.
//   clearDetail()     empty it -- when the tabs change, or when nothing is selected.
//   listingsChanged() the listings were just re-read. What else stood on them is the
//                     frame's business: this column does not know what other columns exist,
//                     and the vocabularies it would otherwise have had to clear are one of
//                     them.
//
// `naming.js` is imported outright, like everywhere else: pure functions over one entry have
// nothing to inject and nothing to reach back into.

import { ownerOf, localName, itemId } from '../../naming.js';

const $ = (id) => document.getElementById(id);

//: The same 12-line shim every module that builds DOM carries; `screens/about.js` says why it
//: is copied rather than shared.
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

//: What this column is handed; see the header. Null until `install`.
let port = null;

/** Wire the column up. Once, from `app.js`. */
export function install(dependencies) {
  port = dependencies;

  document.querySelectorAll('#tabs button').forEach((button) => {
    button.addEventListener('click', () => {
      port.state.kind = button.dataset.kind;
      document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b === button));
      port.state.item = null;
      port.clearDetail();
      render();
    });
  });

  $('filter').addEventListener('input', render);

  // A fixed tooltip does not follow its row, and there is no sensible place for it to be
  // once the thing it points at has moved. Dismissed rather than chased.
  $('primitives').addEventListener('scroll', hideTooltip);
  window.addEventListener('resize', hideTooltip);
}

async function refreshListings() {
  if (port.state.mcp?.state !== 'ready') return;
  const capabilities = port.state.mcp.capabilities || {};
  const ask = (method, key, guard) => (guard === false
    ? Promise.resolve({ [key]: [] })
    : port.state.mcp.request(method).catch(() => ({ [key]: [] })));

  const [tools, prompts, resources, templates] = await Promise.all([
    ask('tools/list', 'tools'),
    ask('prompts/list', 'prompts', !!capabilities.prompts),
    ask('resources/list', 'resources', !!capabilities.resources),
    ask('resources/templates/list', 'resourceTemplates', !!capabilities.resources),
  ]);
  port.state.listings = {
    tools: tools.tools || [],
    prompts: prompts.prompts || [],
    resources: resources.resources || [],
    templates: templates.resourceTemplates || [],
  };
  render();
  // The listings changed, so what stood on them may have too -- what that means is the
  // frame's to say, because this column does not know what else is on the screen.
  port.listingsChanged();
}

function entriesFor(kind) {
  const all = port.state.listings[kind] || [];
  if (!port.state.selected) return [];
  const needle = $('filter').value.trim().toLowerCase();
  return all
    .filter((entry) => ownerOf(kind, entry) === port.state.selected)
    .filter((entry) => !needle || JSON.stringify(entry).toLowerCase().includes(needle));
}

function render() {
  // Every row below is about to be replaced, the hovered one included.
  hideTooltip();
  $('primitives-title').textContent = port.state.selected || 'Primitives';
  for (const kind of Object.keys(port.state.listings)) {
    const count = port.state.selected
      ? (port.state.listings[kind] || []).filter((e) => ownerOf(kind, e) === port.state.selected).length
      : 0;
    document.querySelector(`[data-count="${kind}"]`).textContent = String(count);
  }

  const list = $('primitives');
  list.replaceChildren();
  if (!port.state.selected) {
    list.append(el('li', { class: 'empty', text: 'Select a server in the header.' }));
    port.clearDetail();
    return;
  }
  const entries = entriesFor(port.state.kind);
  if (!entries.length) {
    list.append(el('li', { class: 'empty', text: `No ${port.state.kind} for ${port.state.selected}.` }));
  }
  for (const entry of entries) {
    const id = entry.name || entry.uri || entry.uriTemplate;
    const row = el('li', {
      class: `primitive${port.state.item && itemId(port.state.item.entry) === id ? ' on' : ''}`,
    });
    const button = el('button', { type: 'button', class: 'primitive-main' }, [
      el('span', { class: 'primitive-name', text: localName(port.state.kind, entry) }),
      el('span', { class: 'primitive-note', text: entry.description || entry.title || '' }),
    ]);
    for (const badge of annotationBadges(entry)) button.append(badge);
    tooltipOn(button, entry.description || entry.title || '');
    button.addEventListener('click', () => port.openItem(port.state.kind, entry));
    row.append(button);
    list.append(row);
  }
}

/**
 * The selected server's own entries of one kind, in the backend's own spelling.
 *
 * Handed to the injectable values column, which wants to know what this server publishes
 * without caring how the gateway spells it -- and which has no business filtering a listing
 * by owner itself. `uri` is the gateway's, which is what `resources/read` takes.
 */
function ownListings(kind) {
  if (!port.state.selected) return [];
  return (port.state.listings[kind] || [])
    .filter((entry) => ownerOf(kind, entry) === port.state.selected)
    .map((entry) => ({ name: localName(kind, entry), uri: entry.uri }));
}

// ── The primitive tooltip ──────────────────────────────────────────────────────
//
// A row ellipsizes its description to one line, which on most servers is the first few
// words of a paragraph. The rest of it lives in the detail pane, which is behind a click --
// no help at all to someone still deciding *which* row to click, which is exactly when the
// description is worth reading. So it also pops on hover.
//
// `position: fixed`, and placed by hand, for the same reason the server menu is: the column
// scrolls, and `overflow-y: auto` clips anything positioned inside it. See `placeMenu`.
//
// One node for the page, not one per row. The list is rebuilt on every refresh and on every
// tab switch, so a per-row tooltip would outlive the row it was about -- and would put a
// hundred hidden nodes in the document to say one thing at a time.

//: Long enough that running the pointer down the list does not strobe, short enough not to
//: feel like a wait. Focus skips it: arriving by keyboard is already a deliberate act.
const TOOLTIP_DELAY = 250;
let tooltipTimer = null;

function showTooltip(anchor, text) {
  const tip = $('tooltip');
  tip.textContent = text;
  tip.hidden = false;
  // The button, not the tooltip, is what a screen reader is on; this is what tells it there
  // is a description to read, and `hideTooltip` is what takes the claim back.
  anchor.setAttribute('aria-describedby', 'tooltip');
  placeTooltip(anchor);
}

function hideTooltip() {
  clearTimeout(tooltipTimer);
  tooltipTimer = null;
  const tip = $('tooltip');
  tip.hidden = true;
  //: Every describer, not just the one we think is current: a row removed mid-hover takes
  //: its own attribute with it, but a rebuild that happened between show and hide would
  //: otherwise leave one pointing at a hidden node.
  for (const stale of document.querySelectorAll('[aria-describedby="tooltip"]')) {
    stale.removeAttribute('aria-describedby');
  }
}

/** Below the row, left-aligned to it, and inside the window on all four sides. */
function placeTooltip(anchor) {
  const tip = $('tooltip');
  const row = anchor.getBoundingClientRect();
  // Measured only once it is unhidden and unpinned: a `hidden` element has no box, and one
  // still wearing the last anchor's coordinates would be measured against the wrong edge.
  tip.style.left = '0px';
  tip.style.top = '0px';
  const box = tip.getBoundingClientRect();
  const below = row.bottom + 6;
  // Flipped above rather than squeezed: a tooltip clamped against the bottom edge covers
  // the row it is describing, which is the one thing it must not do.
  const top = below + box.height > window.innerHeight - 8
    ? Math.max(8, row.top - box.height - 6)
    : below;
  tip.style.left = `${Math.max(8, Math.min(row.left, window.innerWidth - box.width - 8))}px`;
  tip.style.top = `${top}px`;
}

/** Wire one row's button to the tooltip. Does nothing when there is nothing to say. */
function tooltipOn(button, text) {
  if (!text) return;
  button.addEventListener('mouseenter', () => {
    clearTimeout(tooltipTimer);
    tooltipTimer = setTimeout(() => showTooltip(button, text), TOOLTIP_DELAY);
  });
  button.addEventListener('focus', () => showTooltip(button, text));
  button.addEventListener('mouseleave', hideTooltip);
  button.addEventListener('blur', hideTooltip);
  // The click rebuilds the list, so the anchor is about to stop existing.
  button.addEventListener('click', hideTooltip);
}

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

export { refreshListings, render, hideTooltip, ownListings };
