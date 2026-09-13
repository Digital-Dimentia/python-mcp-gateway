// The left column's description tooltip.
//
// Its own file because `boot()` is once per process -- see `harness.mjs`. jsdom has no
// layout, so what is asserted here is never how the thing looks: it is which rows get a
// tooltip at all, what it says, who it is announced to, and -- the part with teeth -- that
// it is always taken down again. A tooltip that outlives the row it described is a box of
// stale prose floating over the page with nothing to dismiss it.

import { describe, it, before, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';
import { ZOO } from './zoo.mjs';

let ui;
let document;
let window;

before(async () => {
  ({ ui, document, window } = await boot());
});

after(() => {
  ui.state.admin.close();
  ui.state.mcp.close();
});

//: The gateway namespaces a backend's tools as `server__name`, and the column filters on
//: that prefix. A fixture without it lists nothing at all.
const tool = (local, description) => ({ name: `${ZOO}${'__'}${local}`, description });

const rows = () => [...document.querySelectorAll('#primitives .primitive-main')];
const tip = () => document.getElementById('tooltip');

function list(...tools) {
  ui.state.selected = ZOO;
  ui.state.item = null;
  ui.state.kind = 'tools';
  ui.state.listings = { tools, prompts: [], resources: [], templates: [] };
  ui.renderPrimitives();
}

describe('a primitive row explains itself on hover', () => {
  beforeEach(() => {
    ui.hideTooltip();
    list(
      tool('described', 'Count the animals in one enclosure.\n\nCounts are as of midnight.'),
      tool('bare', ''),
    );
  });

  it('says nothing until something is hovered', () => {
    assert.equal(tip().hidden, true);
  });

  it('shows the whole description, paragraph breaks and all', () => {
    // The row's own note carries the same string -- it is CSS that ellipsizes it, not the
    // DOM -- so what this pins is that the tooltip does not truncate or reflow it on the
    // way through. `white-space: pre-wrap` in the stylesheet is what makes the break show.
    rows()[0].dispatchEvent(new window.Event('focus'));
    assert.equal(tip().hidden, false);
    assert.equal(tip().textContent, rows()[0].querySelector('.primitive-note').textContent);
    assert.match(tip().textContent, /^Count the animals in one enclosure\./);
    assert.ok(tip().textContent.includes('\n\n'), 'the blank line the author wrote survives');
  });

  it('points the row at it, so a screen reader is told there is one', () => {
    rows()[0].dispatchEvent(new window.Event('focus'));
    assert.equal(rows()[0].getAttribute('aria-describedby'), 'tooltip');
  });

  it('gives a row with no description nothing to pop', () => {
    rows()[1].dispatchEvent(new window.Event('focus'));
    assert.equal(tip().hidden, true);
    assert.equal(rows()[1].getAttribute('aria-describedby'), null);
  });

  it('takes the tooltip down on blur, and releases the row with it', () => {
    const row = rows()[0];
    row.dispatchEvent(new window.Event('focus'));
    row.dispatchEvent(new window.Event('blur'));
    assert.equal(tip().hidden, true);
    assert.equal(row.getAttribute('aria-describedby'), null);
  });

  it('dismisses on Escape without needing focus moved', () => {
    rows()[0].dispatchEvent(new window.Event('focus'));
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    assert.equal(tip().hidden, true);
  });

  it('dismisses when the column scrolls out from under it', () => {
    rows()[0].dispatchEvent(new window.Event('focus'));
    document.getElementById('primitives').dispatchEvent(new window.Event('scroll'));
    assert.equal(tip().hidden, true);
  });

  it('leaves nothing describing a row that a rebuild has removed', () => {
    rows()[0].dispatchEvent(new window.Event('focus'));
    // A refresh lands while the pointer is still on a row. The row is replaced; the
    // attribute must not survive on a detached node, and the box must not stay up.
    list(tool('other', 'Something else entirely.'));
    assert.equal(tip().hidden, true);
    assert.equal(document.querySelectorAll('[aria-describedby="tooltip"]').length, 0);
  });

  it('waits before popping on hover, but not on focus', (t) => {
    // Sweeping the pointer down a list of forty tools should not strobe.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    try {
      rows()[0].dispatchEvent(new window.Event('mouseenter'));
      assert.equal(tip().hidden, true, 'not yet');
      t.mock.timers.tick(300);
      assert.equal(tip().hidden, false, 'and now');
    } finally {
      t.mock.timers.reset();
    }
  });

  it('does not pop after the pointer has already left', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    try {
      const row = rows()[0];
      row.dispatchEvent(new window.Event('mouseenter'));
      row.dispatchEvent(new window.Event('mouseleave'));
      t.mock.timers.tick(300);
      assert.equal(tip().hidden, true, 'a tooltip nobody is waiting for any more');
    } finally {
      t.mock.timers.reset();
    }
  });
});
