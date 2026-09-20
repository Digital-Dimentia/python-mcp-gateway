// A backend's own control surface, rendered as DOM rather than run as code.
//
// A tool can name a `ui://` resource (MCP Apps, SEP-1865) that describes a panel. The
// specification's own tier is HTML in a sandboxed iframe; this is a second tier, declared by
// the resource's `profile` parameter, whose document is **JSON and never markup**.
//
// That is the whole security argument, and it is the repo's existing one rather than a new
// one. `markdown.js` says it plainly: there is no `innerHTML` here and no escaping function
// either, because nothing is ever parsed as markup. A panel that arrived as HTML would be
// the first exception to that rule in the project; a panel that arrives as a list of typed
// blocks is not an exception at all. The worst a hostile document can do is be ugly and ask
// for a tool call a human must approve.
//
// It is also the only tier that works in both hosts. The desktop shell's CSP is
// `connect-src ipc:` with no `frame-src`, so it can neither frame a panel nor fetch one from
// the daemon — see `webui.md`. This renders through the socket the window already has.
//
// ## Declines rather than guesses
//
// The doctrine is `schema_form.js`'s, for the same reason: a partial rendering of something
// we did not understand is confidently wrong, and confidently wrong is worse than visibly
// absent. An unrecognised block, a malformed one, or one past the depth or count ceiling
// renders *as its own JSON* with a line saying what declined. The panel degrades block by
// block and never half-renders in silence.

import { renderMarkdown } from './markdown.js';
import { el, pretty, renderToolResult } from './render.js';
import { buildForm } from './schema_form.js';

/** The `profile` this module answers to, as it appears on the resource's mimeType. */
export const PROFILE = 'mcp-app-declarative';

/** Nesting ceiling for `section`. Four is already more than a panel should need. */
export const MAX_DEPTH = 4;

/** Blocks rendered before the rest are summarised. A panel, not a document. */
export const MAX_BLOCKS = 500;

/** Shown where a pointer names nothing. Not an error: absent data is ordinary. */
const MISSING = '—';

/**
 * Resolve an RFC 6901 JSON Pointer against a value.
 *
 * A pointer, deliberately, and not an expression language. There is exactly one thing it can
 * do — walk named keys and array indices — so there is nothing to evaluate, nothing to
 * sandbox, and no way for a document to describe a computation. `undefined` for anything it
 * does not find, including a malformed pointer.
 */
export function pointer(value, path) {
  if (path === undefined || path === null || path === '') return value;
  if (typeof path !== 'string' || !path.startsWith('/')) return undefined;
  let current = value;
  for (const raw of path.slice(1).split('/')) {
    if (current === null || typeof current !== 'object') return undefined;
    // `~1` is `/` and `~0` is `~`, and the order matters: decoding `~0` first would turn
    // `~01` into `/` instead of `~1`.
    const key = raw.replace(/~1/g, '/').replace(/~0/g, '~');
    if (Array.isArray(current)) {
      if (!/^\d+$/.test(key)) return undefined;
      current = current[Number(key)];
    } else {
      current = Object.prototype.hasOwnProperty.call(current, key) ? current[key] : undefined;
    }
  }
  return current;
}

/** What a value looks like in a cell or a field: a string, never markup. */
function display(value) {
  if (value === undefined || value === null) return MISSING;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return pretty(value);
}

/** A block we will not render, shown as itself so the person can see what arrived. */
function decline(block, why) {
  return el('div', { class: 'panel-decline' }, [
    el('p', { class: 'note', text: why }),
    el('pre', { class: 'panel-raw' }, [el('code', { text: pretty(block) })]),
  ]);
}

function renderText(block) {
  return el('p', { class: 'panel-text', text: String(block.text ?? '') });
}

function renderMarkdownBlock(block) {
  // `markdown.js` builds nodes and renders no links and no images, on purpose. A panel gets
  // exactly the same treatment a tool result already gets.
  return el('div', { class: 'panel-markdown' }, [renderMarkdown(String(block.text ?? ''))]);
}

function renderValue(block, data) {
  const value = display(pointer(data, block.path));
  const missing = value === MISSING;
  return el('div', { class: 'panel-value' }, [
    block.label ? el('span', { class: 'panel-label', text: String(block.label) }) : null,
    el('span', { class: missing ? 'panel-datum panel-missing' : 'panel-datum', text: value }),
  ]);
}

function renderKeyValue(block, data) {
  const source = pointer(data, block.path);
  if (source === null || typeof source !== 'object' || Array.isArray(source)) {
    return decline(block, 'A keyvalue block needs its path to name an object.');
  }
  const list = el('dl', { class: 'panel-kv' });
  for (const [key, value] of Object.entries(source)) {
    list.append(el('dt', { text: key }), el('dd', { text: display(value) }));
  }
  return list;
}

function renderTable(block, data) {
  const rows = pointer(data, block.path);
  const columns = Array.isArray(block.columns) ? block.columns : null;
  if (!Array.isArray(rows)) return decline(block, 'A table block needs its path to name an array.');
  if (!columns || !columns.length) return decline(block, 'A table block needs its columns.');

  const head = el('tr');
  for (const column of columns) head.append(el('th', { text: String(column?.label ?? '') }));

  const body = el('tbody');
  for (const row of rows) {
    const line = el('tr');
    for (const column of columns) line.append(el('td', { text: display(pointer(row, column?.path)) }));
    body.append(line);
  }
  return el('table', { class: 'panel-table' }, [el('thead', {}, [head]), body]);
}

function renderJson(block, data) {
  return el('pre', { class: 'panel-raw' }, [el('code', { text: pretty(pointer(data, block.path)) })]);
}

/**
 * Build one block, or a decline in its place.
 *
 * `context` carries the tool result being rendered and the callback an `action` submits
 * through. Passed down rather than imported, so this module reaches nothing global and the
 * tests can drive it with a plain object.
 */
function renderBlock(block, context, depth) {
  if (block === null || typeof block !== 'object' || Array.isArray(block)) {
    return decline(block, 'A block must be an object.');
  }
  switch (block.type) {
    case 'text':
      return renderText(block);
    case 'markdown':
      return renderMarkdownBlock(block);
    case 'value':
      return renderValue(block, context.data);
    case 'keyvalue':
      return renderKeyValue(block, context.data);
    case 'table':
      return renderTable(block, context.data);
    case 'json':
      return renderJson(block, context.data);
    case 'content':
      // The existing rendering of a tool result, embedded. A panel that wants to show what
      // the call actually returned should not have to restate it.
      return el('div', { class: 'panel-content' }, [renderToolResult(context.result)]);
    case 'section':
      return renderSection(block, context, depth);
    case 'action':
      return renderAction(block, context);
    default:
      return decline(block, `This panel uses a "${block.type}" block, which this version does not render.`);
  }
}

function renderSection(block, context, depth) {
  if (depth >= MAX_DEPTH) {
    return decline(block, `Sections are nested more than ${MAX_DEPTH} deep, which this version does not render.`);
  }
  const blocks = Array.isArray(block.blocks) ? block.blocks : [];
  const section = el('section', { class: 'panel-section' }, [
    block.title ? el('h4', { class: 'panel-section-title', text: String(block.title) }) : null,
  ]);
  for (const child of blocks) section.append(renderBlock(child, context, depth + 1));
  return section;
}

/**
 * A form that calls one of this backend's tools.
 *
 * The form itself is `schema_form.buildForm`, which is the reason this tier is worth having:
 * a declarative action inherits the whole JSON Schema renderer, *including every construct
 * it already declines* and its raw-JSON fallback. Nothing about schemas is reimplemented
 * here, so a panel cannot be more capable than the form column beside it.
 *
 * Submitting does not call anything. It hands the request to `context.onAction`, which owns
 * scope, consent and budget — see `panel_consent.js`. A panel proposes; a person disposes.
 */
function renderAction(block, context) {
  const tool = block.tool;
  if (typeof tool !== 'string' || !tool) return decline(block, 'An action block needs a tool name.');

  const form = buildForm(block.schema ?? { type: 'object', properties: {} });
  const button = el('button', { class: 'btn', type: 'button', text: String(block.label || tool) });
  const note = el('p', { class: 'note panel-action-note' });

  button.addEventListener('click', async () => {
    let args;
    try {
      args = form.collect();
    } catch (error) {
      note.textContent = String(error?.message || error);
      return;
    }
    button.disabled = true;
    note.textContent = '';
    try {
      await context.onAction({ tool, arguments: args, confirm: block.confirm });
    } catch (error) {
      // A refusal is an ordinary outcome here, not a failure of the panel.
      note.textContent = String(error?.message || error);
    } finally {
      button.disabled = false;
    }
  });

  return el('div', { class: 'panel-action' }, [
    form.element,
    el('div', { class: 'panel-action-bar' }, [button]),
    note,
  ]);
}

/**
 * Render a panel document, or say why it will not render.
 *
 * `document_` is whatever `resources/read` returned, already parsed. `result` is the tool
 * result the panel is about; its `structuredContent` is what pointers resolve against,
 * falling back to the whole result so a panel still works against a server that sends none.
 */
export function renderPanel(document_, { result = {}, onAction = async () => {} } = {}) {
  if (document_ === null || typeof document_ !== 'object' || Array.isArray(document_)) {
    return decline(document_, 'A panel document must be a JSON object.');
  }
  if (document_.panel !== 1) {
    return decline(
      { panel: document_.panel },
      'This panel declares a version this build does not render.',
    );
  }

  const blocks = Array.isArray(document_.blocks) ? document_.blocks : [];
  const context = {
    result,
    data: result?.structuredContent ?? result,
    onAction,
  };

  const root = el('div', { class: 'panel' }, [
    document_.title ? el('h3', { class: 'panel-title', text: String(document_.title) }) : null,
  ]);
  for (const block of blocks.slice(0, MAX_BLOCKS)) root.append(renderBlock(block, context, 0));
  if (blocks.length > MAX_BLOCKS) {
    root.append(
      el('p', {
        class: 'note',
        text: `${blocks.length - MAX_BLOCKS} further block(s) not shown: a panel is a control surface, not a document.`,
      }),
    );
  }
  return root;
}

/** Whether a resource's mimeType claims this tier. */
export function isDeclarativePanel(mimeType) {
  if (typeof mimeType !== 'string') return false;
  return mimeType.split(';').slice(1).some((part) => {
    const [key, value] = part.split('=');
    return key.trim().toLowerCase() === 'profile' && value?.trim().replace(/"/g, '') === PROFILE;
  });
}
