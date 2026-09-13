// The Results column's per-card delete.
//
// Its own file because `boot()` is once per process -- see `harness.mjs`. What is worth a
// test here is not the button (a button is looked at, not asserted) but the bookkeeping
// around it: that removing one card leaves the others alone, and that the column knows when
// it has just lost its last one. The empty-state paragraph is the only thing standing
// between an emptied column and something that reads as broken.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

let ui;
let document;

before(async () => {
  ({ ui, document } = await boot());
});

// Both sockets reconnect on their own and every request carries a 120s timeout, so a suite
// that just ends leaves real timers behind. See the same note in `shell.test.mjs`.
after(() => {
  ui.state.admin.close();
  ui.state.mcp.close();
});

const results = () => document.getElementById('results');
const cards = () => [...results().querySelectorAll('.card')];
const titles = () => cards().map((card) => card.querySelector('h3').textContent);

function push(title) {
  ui.pushCard({
    title,
    subtitle: '',
    request: { method: title, params: {} },
    body: document.createElement('div'),
    elapsedMs: 1,
    raw: { ok: true },
  });
}

describe('every result card can be deleted on its own', () => {
  before(() => {
    ui.clearResults();
    push('first');
    push('second');
    push('third');
  });

  it('starts with three cards and no empty state', () => {
    // `pushCard` prepends, so the newest is first.
    assert.deepEqual(titles(), ['third', 'second', 'first']);
    assert.equal(results().querySelector('.empty'), null);
  });

  it('gives each card a trashcan button carrying a drawn icon', () => {
    for (const card of cards()) {
      const button = card.querySelector('.card-del');
      assert.ok(button, 'every card has a delete button');
      assert.equal(button.getAttribute('type'), 'button');
      // In the SVG namespace, or the browser parses it as HTML and draws nothing.
      const svg = button.querySelector('svg');
      assert.equal(svg.namespaceURI, 'http://www.w3.org/2000/svg');
      assert.ok(svg.querySelectorAll('path').length > 1);
      // Hidden from the tree that reads it: the button's own label says what it does.
      assert.equal(svg.getAttribute('aria-hidden'), 'true');
      assert.match(card.querySelector('.card-del').getAttribute('aria-label'), /^Remove the /);
    }
  });

  it('removes only the card whose button was clicked', () => {
    cards()[1].querySelector('.card-del').click();
    assert.deepEqual(titles(), ['third', 'first']);
    assert.equal(results().querySelector('.empty'), null);
  });

  it('restores the empty state when the last card goes', () => {
    for (const card of cards()) card.querySelector('.card-del').click();
    assert.deepEqual(titles(), []);
    const empty = results().querySelector('.empty');
    assert.ok(empty, 'an emptied column says it is empty rather than showing nothing');
    // The same sentence the Clear button leaves behind, and the same one `index.html` ships.
    assert.equal(empty.textContent, 'Invoke a tool, get a prompt, or read a resource.');
  });

  it('takes a new card after the column has been emptied', () => {
    push('fourth');
    assert.deepEqual(titles(), ['fourth']);
    assert.equal(results().querySelector('.empty'), null);
  });
});
