// The declarative panel tier: what it renders, and what it refuses to guess at.
//
// The load-bearing test here is `no block ever assigns innerHTML`. Everything else is a
// rendering rule; that one is the reason this tier exists at all, and it fails the moment
// somebody reaches for a shortcut.

import assert from 'node:assert/strict';
import test from 'node:test';

import { boot } from './harness.mjs';

const { window } = await boot();
const {
  renderPanel,
  pointer,
  isDeclarativePanel,
  MAX_DEPTH,
  MAX_BLOCKS,
} = await import('../../src/mcp_gateway_ui/panel_declarative.js');

const panel = (blocks, extra = {}) => ({ panel: 1, blocks, ...extra });
const render = (document_, options) => renderPanel(document_, options);
const textOf = (node) => node.textContent.replace(/\s+/g, ' ').trim();

// --- JSON Pointer ---------------------------------------------------------------

test('a pointer walks objects and array indices', () => {
  const data = { a: { b: [10, 20] } };
  assert.equal(pointer(data, '/a/b/1'), 20);
  assert.deepEqual(pointer(data, '/a'), { b: [10, 20] });
  assert.equal(pointer(data, ''), data);
});

test('a pointer decodes ~1 as slash and ~0 as tilde, in that order', () => {
  assert.equal(pointer({ 'a/b': 1 }, '/a~1b'), 1);
  assert.equal(pointer({ '~1': 2 }, '/~01'), 2);
});

test('a pointer that misses returns undefined rather than throwing', () => {
  assert.equal(pointer({ a: 1 }, '/nope'), undefined);
  assert.equal(pointer({ a: 1 }, '/a/b/c'), undefined);
  assert.equal(pointer([1], '/notanindex'), undefined);
  assert.equal(pointer({ a: 1 }, 'no-leading-slash'), undefined);
});

test('a pointer reads only own properties, never the prototype chain', () => {
  assert.equal(pointer({}, '/constructor'), undefined);
  assert.equal(pointer({}, '/__proto__'), undefined);
});

// --- rendering ------------------------------------------------------------------

test('a text block renders its text and nothing else', () => {
  const node = render(panel([{ type: 'text', text: 'hello <b>there</b>' }]));
  assert.equal(textOf(node), 'hello <b>there</b>');
  assert.equal(node.querySelector('b'), null, 'markup in a value is text, not markup');
});

test('a value block resolves its pointer against structuredContent', () => {
  const node = render(panel([{ type: 'value', label: 'Temp', path: '/temperature' }]), {
    result: { structuredContent: { temperature: 72 } },
  });
  assert.match(textOf(node), /Temp\s*72/);
});

test('a value block whose pointer misses shows a placeholder and does not fail the panel', () => {
  const node = render(panel([
    { type: 'value', path: '/absent' },
    { type: 'text', text: 'still here' },
  ]), { result: { structuredContent: {} } });
  assert.match(textOf(node), /still here/, 'a miss must not take the rest of the panel with it');
  assert.equal(node.querySelectorAll('.panel-missing').length, 1);
});

test('a table renders one row per entry and one cell per column', () => {
  const node = render(panel([
    { type: 'table', path: '/rows', columns: [{ label: 'Name', path: '/name' }] },
  ]), { result: { structuredContent: { rows: [{ name: 'ada' }, { name: 'grace' }] } } });
  assert.equal(node.querySelectorAll('tbody tr').length, 2);
  assert.match(textOf(node), /ada.*grace/);
});

test('a table whose path is not an array declines', () => {
  const node = render(panel([{ type: 'table', path: '/rows', columns: [] }]), {
    result: { structuredContent: { rows: 'nope' } },
  });
  assert.equal(node.querySelectorAll('.panel-decline').length, 1);
});

test('a keyvalue block renders a definition list', () => {
  const node = render(panel([{ type: 'keyvalue', path: '/meta' }]), {
    result: { structuredContent: { meta: { host: 'box', port: 8765 } } },
  });
  assert.equal(node.querySelectorAll('dt').length, 2);
  assert.match(textOf(node), /host\s*box/);
});

test('a section nests its blocks', () => {
  const node = render(panel([
    { type: 'section', title: 'Inside', blocks: [{ type: 'text', text: 'nested' }] },
  ]));
  assert.match(textOf(node), /Inside\s*nested/);
});

// --- declines -------------------------------------------------------------------

test('an unknown block type declines to raw JSON rather than guessing', () => {
  const node = render(panel([{ type: 'hologram', spin: true }]));
  const decline = node.querySelector('.panel-decline');
  assert.ok(decline, 'an unknown block must be visible, not dropped');
  assert.match(textOf(decline), /hologram/, 'and must show what actually arrived');
});

test('a block that is not an object declines', () => {
  const node = render(panel(['just a string', 42, null]));
  assert.equal(node.querySelectorAll('.panel-decline').length, 3);
});

test('nesting deeper than the ceiling declines rather than recursing', () => {
  let deepest = { type: 'text', text: 'bottom' };
  for (let i = 0; i <= MAX_DEPTH + 1; i += 1) {
    deepest = { type: 'section', blocks: [deepest] };
  }
  const node = render(panel([deepest]));
  assert.ok(node.querySelector('.panel-decline'), 'the ceiling must show itself');
});

test('a document that is not a panel declines', () => {
  assert.ok(render({ nope: true }).querySelector, 'still returns a node');
  assert.match(textOf(render({ nope: true })), /must be a JSON object|does not render/);
  assert.match(textOf(render(null)), /must be a JSON object/);
  assert.match(textOf(render({ panel: 99, blocks: [] })), /version this build does not render/);
});

test('a panel of too many blocks is truncated with a note rather than rendered whole', () => {
  const many = Array.from({ length: MAX_BLOCKS + 5 }, () => ({ type: 'text', text: 'x' }));
  const node = render(panel(many));
  assert.match(textOf(node), /further block\(s\) not shown/);
});

// --- the rule the tier exists for -----------------------------------------------

test('no block ever assigns innerHTML', () => {
  const proto = window.Element.prototype;
  const original = Object.getOwnPropertyDescriptor(proto, 'innerHTML');
  let assignments = 0;
  Object.defineProperty(proto, 'innerHTML', {
    configurable: true,
    get: original.get,
    set(value) {
      assignments += 1;
      original.set.call(this, value);
    },
  });
  try {
    render(panel([
      { type: 'text', text: '<img src=x onerror=alert(1)>' },
      { type: 'markdown', text: '<script>alert(1)</script>' },
      { type: 'json', path: '/x' },
      { type: 'keyvalue', path: '/m' },
      { type: 'table', path: '/r', columns: [{ label: '<b>', path: '/c' }] },
      { type: 'hologram' },
      { type: 'section', blocks: [{ type: 'text', text: '<b>x</b>' }] },
    ]), { result: { structuredContent: { x: 1, m: { a: 1 }, r: [{ c: '<i>' }] } } });
  } finally {
    Object.defineProperty(proto, 'innerHTML', original);
  }
  assert.equal(assignments, 0, 'a panel document is data, and data is never parsed as markup');
});

test('a hostile string in a table cell stays a string', () => {
  const node = render(panel([
    { type: 'table', path: '/r', columns: [{ label: 'C', path: '/c' }] },
  ]), { result: { structuredContent: { r: [{ c: '<script>alert(1)</script>' }] } } });
  assert.equal(node.querySelector('script'), null);
  assert.match(textOf(node), /alert\(1\)/, 'shown as text, so the person can see it');
});

// --- actions --------------------------------------------------------------------

test('an action hands its request to the host rather than calling anything', async () => {
  const seen = [];
  const node = render(panel([
    { type: 'action', tool: 'zoo__board', label: 'Go', schema: { type: 'object', properties: {} } },
  ]), { onAction: async (request) => seen.push(request) });

  node.querySelector('.panel-action .btn').click();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(seen.length, 1);
  assert.equal(seen[0].tool, 'zoo__board');
});

test('an action without a tool name declines', () => {
  const node = render(panel([{ type: 'action', label: 'Go' }]));
  assert.ok(node.querySelector('.panel-decline'));
});

test('a refusal from the host is shown on the action, not thrown away', async () => {
  const node = render(panel([
    { type: 'action', tool: 'zoo__board', schema: { type: 'object', properties: {} } },
  ]), {
    onAction: async () => {
      throw new Error('refused by the operator');
    },
  });
  node.querySelector('.panel-action .btn').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.match(textOf(node.querySelector('.panel-action-note')), /refused by the operator/);
});

test('an action inherits schema_form’s decline for a schema it will not render', () => {
  const node = render(panel([
    {
      type: 'action',
      tool: 'zoo__board',
      schema: { type: 'object', if: { properties: {} }, then: { required: ['a'] } },
    },
  ]));
  // Not our decline: the form's own raw-JSON fallback, inherited whole.
  assert.ok(node.querySelector('.panel-action textarea'), 'the form falls back to raw JSON');
});

// --- the profile ----------------------------------------------------------------

test('the tier is chosen by the resource mimeType, not by the tool', () => {
  assert.ok(isDeclarativePanel('application/json;profile=mcp-app-declarative'));
  assert.ok(isDeclarativePanel('application/json; profile="mcp-app-declarative"'));
  assert.equal(isDeclarativePanel('text/html;profile=mcp-app'), false);
  assert.equal(isDeclarativePanel('application/json'), false);
  assert.equal(isDeclarativePanel(undefined), false);
});
