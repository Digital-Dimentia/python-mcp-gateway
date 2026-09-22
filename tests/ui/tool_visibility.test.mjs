// Which of a server's tools `/mcp` advertises: the checkbox list in the header menu.
//
// What is pinned is the part a person cannot see go wrong: which set goes over the wire
// when a box is ticked. The payload is the *whole* hidden set for one server, every time --
// see `src/mcp_gateway/visibility.md` -- so a tick that sent only the one name, or a Select
// none that missed a tool, would look right in the menu and publish the wrong thing.
//
// And the coupling: the list is built from this page's own `tools/list`, which the daemon
// leaves unfiltered for the bench precisely so a hidden tool can be offered here at all.
//
// Not tested, because jsdom has no layout: how tall the menu grows to hold the whole list.

import { describe, it, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

//: One booted page for the whole file -- `harness.mjs` says why it cannot be one per suite.
let ui;
let document;
before(async () => {
  ({ ui, document } = await boot());
});

describe('the tool picker in a server menu', () => {
  //: What went over `/admin`, in order.
  let sent;
  //: What the stubbed daemon currently has hidden, so the refresh after a write reads back
  //: what the write said -- the same round trip the real page makes.
  let hidden;

  const backend = () => ({
    name: 'zoo', status: 'running', enabled: true, hidden_tools: [...hidden],
  });

  const openMenu = () => {
    const caret = [...document.querySelectorAll('#servers .server')]
      .find((w) => w.querySelector('.server-name')?.textContent === 'zoo')
      .querySelector('.server-caret');
    caret.click();
    return document.getElementById('server-menu-zoo');
  };

  const hides = () => sent.filter((call) => call.method === 'admin.tools.hide');

  beforeEach(() => {
    sent = [];
    hidden = ['b'];
    ui.state.admin = {
      request: (method, params) => {
        sent.push({ method, params });
        if (method === 'admin.tools.hide') hidden = [...params.tools];
        if (method === 'admin.backends') return Promise.resolve({ backends: [backend()] });
        return Promise.resolve({});
      },
    };
    ui.state.backends = [backend()];
    ui.state.listings = {
      tools: [{ name: 'zoo__a' }, { name: 'zoo__b' }, { name: 'zoo__c' }],
      prompts: [], resources: [], templates: [],
    };
    // Anything a previous test left open is put away by rebuilding with the menu shut.
    document.body.click();
    ui.renderBackends();
  });

  it('lists every tool the server has, ticked unless hidden', () => {
    const menu = openMenu();
    const rows = [...menu.querySelectorAll('.server-tools li.field-tick')];
    assert.deepEqual(rows.map((r) => r.querySelector('label').textContent), ['a', 'b', 'c']);
    assert.deepEqual(rows.map((r) => r.querySelector('input').checked), [true, false, true]);
    assert.equal(menu.querySelector('.server-tools-head .badge').textContent, '2 of 3');
  });

  it('wires each label to its own box, with unique ids', () => {
    const menu = openMenu();
    const rows = [...menu.querySelectorAll('.server-tools li.field-tick')];
    for (const row of rows) {
      const box = row.querySelector('input[type="checkbox"]');
      assert.ok(box.id);
      assert.equal(row.querySelector('label').htmlFor, box.id);
    }
    const ids = rows.map((r) => r.querySelector('input').id);
    assert.equal(new Set(ids).size, ids.length);
  });

  it('sends the whole hidden set when one box is unticked', async () => {
    const menu = openMenu();
    const box = [...menu.querySelectorAll('.server-tools input')][0];
    box.checked = false;
    box.dispatchEvent(new window.Event('change'));
    await tick();
    assert.deepEqual(hides(), [{ method: 'admin.tools.hide', params: { name: 'zoo', tools: ['a', 'b'] } }]);
  });

  it('sends every name for None, and nothing for All', async () => {
    let menu = openMenu();
    const button = (label) => [...menu.querySelectorAll('.server-tools-head button')]
      .find((b) => b.textContent === label);

    button('None').click();
    await tick();
    menu = document.getElementById('server-menu-zoo');
    button('All').click();
    await tick();

    assert.deepEqual(hides().map((call) => call.params), [
      { name: 'zoo', tools: ['a', 'b', 'c'] },
      { name: 'zoo', tools: [] },
    ]);
  });

  it('stays open, and re-reads what the daemon says, after a tick', async () => {
    const menu = openMenu();
    [...menu.querySelectorAll('.server-tools input')][2].click();
    await tick();
    const after = document.getElementById('server-menu-zoo');
    assert.equal(after.hidden, false);
    assert.equal(after.querySelector('.server-tools-head .badge').textContent, '1 of 3');
  });

  it('does not push a result card for every tick', async () => {
    const before = document.querySelectorAll('#results .card').length;
    const menu = openMenu();
    [...menu.querySelectorAll('.server-tools input')][0].click();
    await tick();
    assert.equal(document.querySelectorAll('#results .card').length, before);
  });

  it('offers the hidden names even when nothing is listed, so a hide can always be undone', () => {
    ui.state.listings.tools = [];
    ui.renderBackends();
    const menu = openMenu();
    const rows = [...menu.querySelectorAll('.server-tools li.field-tick')];
    assert.deepEqual(rows.map((r) => r.querySelector('label').textContent), ['b']);
    assert.equal(rows[0].querySelector('input').checked, false);
  });

  const pillCount = (name) => [...document.querySelectorAll('#servers .server')]
    .find((w) => w.querySelector('.server-name')?.textContent === name)
    ?.querySelector('.server-count');

  it('counts on the pill what /mcp advertises, and says out of how many on hover', () => {
    const badge = pillCount('zoo');
    assert.equal(badge.textContent, '2');
    assert.equal(badge.title, '2 of 3 tools advertised on /mcp');
    assert.ok(badge.classList.contains('server-count-trimmed'));
  });

  it('is not accented when nothing is hidden', () => {
    hidden = [];
    ui.state.backends = [backend()];
    ui.renderBackends();
    assert.equal(pillCount('zoo').textContent, '3');
    assert.ok(!pillCount('zoo').classList.contains('server-count-trimmed'));
  });

  it('follows a tick without the menu having to be reopened', async () => {
    const menu = openMenu();
    [...menu.querySelectorAll('.server-tools input')][0].click();
    await tick();
    assert.equal(pillCount('zoo').textContent, '1');
  });

  it('shows no count, rather than 0, for a server nothing is listed for', () => {
    ui.state.listings.tools = [];
    ui.renderBackends();
    assert.equal(pillCount('zoo'), null);
  });

  it('counts the meta-tools on the gateway pill', () => {
    ui.state.listings.tools = [...ui.state.listings.tools, { name: 'gateway__list_backends' }];
    ui.renderBackends();
    assert.equal(document.querySelector('#servers .server-meta .server-count').textContent, '1');
  });

  it('has no picker on the gateway pill: the meta-tools are not selectable', () => {
    const meta = document.querySelector('#servers .server-meta');
    assert.equal(meta.querySelector('.server-tools'), null);
  });

});

describe('the primitives column', () => {
  it('marks a hidden tool, and still lists it', () => {
    ui.state.backends = [{ name: 'zoo', status: 'running', enabled: true, hidden_tools: ['b'] }];
    ui.state.listings = {
      tools: [{ name: 'zoo__a' }, { name: 'zoo__b' }],
      prompts: [], resources: [], templates: [],
    };
    ui.state.kind = 'tools';
    ui.select('zoo');
    ui.renderPrimitives();

    const rows = [...document.querySelectorAll('#primitives .primitive')];
    assert.equal(rows.length, 2, 'the bench lists every tool, hidden or not');
    const [a, b] = rows;
    assert.equal(a.querySelector('.badge-hidden'), null);
    assert.ok(b.querySelector('.badge-hidden'));
    assert.ok(b.classList.contains('primitive-hidden'));
  });
});
