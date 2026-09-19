// The server editor's field grouping.
//
// The editor used to be one flat list, and adding a key to it meant adding one line. Now
// the keys live in three groups, and the failure mode that introduces is silent: a key
// added to the spec but to none of the groups simply never appears in the dialog, and a key
// left in two groups renders twice and collects itself twice. Neither shows up as an error.
// So the partition is what is pinned here -- every key the collector reads is in exactly one
// column -- plus the fact that opening the dialog does produce the three columns.
//
// Not tested, because jsdom has no layout: that the three columns sit side by side, or that
// the dialog no longer scrolls. That is what looking at it says.

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

//: Every key `admin.backend.add` accepts, which is what the editor is a form over.
const SPEC_KEYS = [
  'command', 'url', 'args', 'env', 'env_passthrough', 'headers', 'cwd',
  'description', 'timeout', 'startup_timeout', 'enabled', 'required',
];

describe('the server editor', () => {
  let ui;
  let document;

  before(async () => {
    ({ ui, document } = await boot());
  });

  it('puts every spec key in exactly one group', () => {
    const keys = ui.EDITOR_GROUPS.flatMap(([, fields]) => fields.map(([key]) => key));
    assert.deepEqual([...keys].sort(), [...SPEC_KEYS].sort());
    assert.equal(new Set(keys).size, keys.length);
  });

  it('lays the groups out in three columns', () => {
    ui.openEditor(null);
    const columns = document.querySelectorAll('#server-form .editor-column');
    assert.equal(columns.length, 3);
    const headings = [...columns].map((c) => c.querySelector('.editor-heading').textContent);
    assert.deepEqual(headings, ui.EDITOR_GROUPS.map(([heading]) => heading));
  });

  it('renders a control for every key, and a name field only when adding', () => {
    ui.openEditor(null);
    const adding = [...document.querySelectorAll('#server-form .field-name')].map((n) => n.textContent);
    assert.deepEqual(adding, ['name', ...ui.EDITOR_GROUPS.flatMap(([, f]) => f.map(([k]) => k))]);

    ui.state.config = { servers: { zoo: { command: 'python', args: ['-m', 'zoo'] } } };
    ui.openEditor('zoo');
    const editing = [...document.querySelectorAll('#server-form .field-name')].map((n) => n.textContent);
    assert.ok(!editing.includes('name'));
    assert.equal(editing.length, SPEC_KEYS.length);
  });

  it('puts a boolean on one line, with its label wired to the box', () => {
    ui.state.config = { servers: { zoo: { command: 'python' } } };
    ui.openEditor('zoo');
    const ticks = [...document.querySelectorAll('#server-form .field-tick')];
    assert.deepEqual(
      ticks.map((f) => f.querySelector('.field-name').textContent),
      ['enabled', 'required'],
    );
    for (const tick of ticks) {
      const box = tick.querySelector('input[type="checkbox"]');
      // The label points at the box, so clicking the word toggles it.
      assert.equal(tick.querySelector('label').htmlFor, box.id);
      assert.ok(box.id);
    }
    // Ids are unique across a form, and across re-openings of one.
    const ids = [...document.querySelectorAll('#server-form input[type="checkbox"]')].map((b) => b.id);
    assert.equal(new Set(ids).size, ids.length);
    // Every other field is still stacked.
    assert.equal(document.querySelectorAll('#server-form .field').length - ticks.length, 10);
  });

  it("prefers the file's own defaults: block to the built-in fallback", () => {
    ui.state.config = { servers: {}, defaults: { timeout: 45, cwd: '/srv' } };
    ui.openEditor(null);
    const fields = [...document.querySelectorAll('#server-form .field')];
    const by = (key) => fields.find((f) => f.querySelector('.field-name').textContent === key);
    assert.equal(by('timeout').querySelector('input').placeholder, '45');
    assert.equal(by('cwd').querySelector('input').placeholder, '/srv');
    // A key the block is silent about still falls back to the daemon's own.
    assert.equal(by('startup_timeout').querySelector('input').placeholder, '20');
    ui.state.config = { servers: {} };
  });

  it('shows the daemon defaults as placeholders, and writes neither', async () => {
    ui.state.config = { servers: {} };
    ui.openEditor(null);
    const fields = [...document.querySelectorAll('#server-form .field')];
    const by = (key) => fields.find((f) => f.querySelector('.field-name').textContent === key);
    assert.equal(by('timeout').querySelector('input').placeholder, '30');
    assert.equal(by('startup_timeout').querySelector('input').placeholder, '20');
    assert.equal(by('timeout').querySelector('input').value, '');
    assert.equal(by('env').querySelector('textarea').placeholder, 'API_KEY=${FILES_API_KEY}');

    // A form left alone sends no timeout at all, so the file's own `defaults:` block --
    // which the UI cannot see -- still wins. A prefilled value would have overridden it.
    by('name').querySelector('input').value = 'files';
    by('command').querySelector('input').value = 'npx';
    const sent = [];
    ui.state.admin = { request: (method, params) => { sent.push({ method, params }); return null; } };
    document.querySelector('#server-form .detail-actions button').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    // `admin()` refetches after a write, so the add is the first of several calls.
    const add = sent.find((call) => call.method === 'admin.backend.add');
    assert.ok(add, 'the form sent an add');
    assert.ok(!('timeout' in add.params.spec));
    assert.ok(!('startup_timeout' in add.params.spec));
    assert.equal(add.params.spec.enabled, true);
    assert.equal(add.params.spec.required, false);
    assert.equal(add.params.name, 'files');
  });

  it('fills the controls from the existing spec', () => {
    ui.state.config = { servers: { zoo: { command: 'python', args: ['-m', 'zoo'], enabled: false } } };
    ui.openEditor('zoo');
    const fields = [...document.querySelectorAll('#server-form .field')];
    const by = (key) => fields.find((f) => f.querySelector('.field-name').textContent === key);
    assert.equal(by('command').querySelector('input').value, 'python');
    assert.equal(by('args').querySelector('textarea').value, '-m\nzoo');
    assert.equal(by('enabled').querySelector('input').checked, false);
  });
});
