// Emptying the detail panel is three things, and every caller wants all three.
//
// The bug this pins: switching servers nulled `state.item` and left the form on screen. It
// was still live -- its send button worked and its closure held the *old* server's entry --
// while `variables.refreshVariables()` had just repopulated the Injectable values column for
// the new server, so that column's picks were written into a form belonging to a server you
// were no longer looking at.
//
// The fix was not the line `select` forgot. It was that there were two lines to remember at
// all: `clear` now owns the DOM, `sendControl` and `state.item` together, so there is no
// pair left for a fourth caller to do half of. These tests assert the pair agrees, not just
// that one symptom went away.

import assert from 'node:assert/strict';
import test from 'node:test';

import { boot } from './harness.mjs';

const { ui, document } = await boot();
// The same module instance `app.js` installed: ESM caches by resolved URL, and this path
// and the frame's `new URL(...)` both resolve to the one file.
const detail = await import('../../src/mcp_gateway_ui/screens/basics/detail.js');

const $ = (id) => document.getElementById(id);

const tool = (name) => ({
  name,
  description: `${name} does a thing`,
  inputSchema: { type: 'object', properties: { text: { type: 'string' } } },
});

/** The state a person is in after clicking a tool on `server`: a selection and a form. */
function openToolOn(server) {
  ui.state.selected = server;
  ui.state.kind = 'tools';
  ui.state.listings = {
    tools: [tool(`${server}__alpha`)],
    prompts: [],
    resources: [],
    templates: [],
  };
  detail.openItem('tools', ui.state.listings.tools[0]);
  assert.ok($('detail').children.length > 0, 'precondition: a form is open');
  assert.ok(ui.state.item, 'precondition: something is selected');
}

const cleared = () => ({
  view: $('detail').children.length === 0,
  selection: ui.state.item === null,
});

test('opening a tool puts a form in the panel and records the selection', () => {
  openToolOn('zoo');
  assert.equal(ui.state.item.entry.name, 'zoo__alpha');
  assert.ok($('detail').querySelector('button'), 'the form brings its send button');
});

test('switching servers empties the panel', () => {
  openToolOn('zoo');
  ui.select('other');

  assert.deepEqual(cleared(), { view: true, selection: true });
  assert.equal(ui.state.selected, 'other');
});

test('the view and the selection always agree afterwards', () => {
  // The regression is not "select forgot a line", it is "there were two lines". A panel with
  // nothing in it and a live selection is as broken as a live form and no selection.
  openToolOn('zoo');
  ui.select('other');
  const { view, selection } = cleared();
  assert.equal(view, selection, 'a half-cleared panel is the bug');
});

test('reselecting the same server clears too, rather than being a special case', () => {
  openToolOn('zoo');
  ui.select('zoo');
  assert.deepEqual(cleared(), { view: true, selection: true });
});

test('switching tabs still clears, now that it delegates the selection as well', () => {
  openToolOn('zoo');
  const prompts = document.querySelector('#tabs button[data-kind="prompts"]');
  assert.ok(prompts, 'the page has a prompts tab');
  prompts.click();

  assert.deepEqual(cleared(), { view: true, selection: true });
  assert.equal(ui.state.kind, 'prompts');
});

test('clear is safe to call when nothing is open', () => {
  detail.clear();
  detail.clear();
  assert.deepEqual(cleared(), { view: true, selection: true });
});
