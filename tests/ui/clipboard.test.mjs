// The Clipboard button, the modal, and the snapshot behind both.
//
// Its own file because `boot()` is once per process -- see `harness.mjs`.
//
// What is worth asserting here is the *snapshot*, not the document: the text is rendered by
// `clipboard.py` and pinned by `tests/test_clipboard.py`, and the one thing no Python test
// can reach is whether the page describes its own two columns correctly. A snapshot that
// quietly disagreed with what is on screen would produce a perfectly well-formed briefing
// about a session that did not happen.
//
// The rest is the editor's one rule: what the Copy button puts on the clipboard is what is
// in the box, edits included, and a card arriving behind an edited box does not throw that
// edit away.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot, selectServer, mcpAnswering, enumBody, settle } from './harness.mjs';

let ui;
let document;
let window;

//: Every `admin.clipboard.put` the page has made, and what the daemon pretends to answer.
let published = [];
let answer = '## Results\n\nrendered by the daemon\n';

//: Everything the page has put on the clipboard, in order.
const clipboard = { copied: [], async writeText(text) { this.copied.push(text); } };

before(async () => {
  ({ ui, document, window } = await boot());

  // The `/admin` socket, stubbed at the one method the clipboard uses. `installClipboard`
  // reads `state.admin` at call time, so replacing it here is enough.
  ui.state.admin = {
    async request(method, params) {
      if (method !== 'admin.clipboard.put') throw new Error(`unexpected ${method}`);
      published.push(params.snapshot);
      return { document: answer, captured_at: 1, empty: false };
    },
  };

  // jsdom ships no clipboard. Recorded rather than stubbed away, because "what landed on
  // the clipboard" is the assertion.
  //
  // Installed on *both* navigators, and that is not belt-and-braces. `harness.mjs` copies
  // the window's globals onto Node's, but `navigator` is a read-only global from Node 21 on,
  // so that copy silently fails and the modules keep seeing Node's own. The page reaches for
  // a bare `navigator`; a test that patched only jsdom's would watch the wrong object and
  // see the copy land in the catch branch.
  for (const nav of new Set([window.navigator, globalThis.navigator])) {
    Object.defineProperty(nav, 'clipboard', { configurable: true, value: clipboard });
  }
});

after(() => {
  ui.state.mcp?.close?.();
});

const $ = (id) => document.getElementById(id);

function push(title, { failed = false } = {}) {
  ui.pushCard({
    title,
    subtitle: 'tools/call',
    request: { method: 'tools/call', params: { name: title, arguments: { n: 1 } } },
    body: document.createElement('div'),
    elapsedMs: 7,
    raw: { content: [{ type: 'text', text: 'ok' }] },
    failed,
  });
}

/** Let the click handlers this module fires and forgets actually run. */
async function settleClipboard() {
  for (let i = 0; i < 50; i += 1) await new Promise((resume) => { setImmediate(resume); });
}

describe('the Clipboard button sits in the Results column', () => {
  it('is a button, next to Clear, and says what it is for', () => {
    const button = $('btn-clipboard');
    assert.ok(button, 'the Results column has a Clipboard button');
    assert.equal(button.getAttribute('type'), 'button');
    assert.equal(button.closest('.col-results')?.tagName, 'SECTION');
    assert.match(button.getAttribute('title'), /agent/);
  });
});

describe('the snapshot describes the Results column', () => {
  before(() => {
    ui.clearResults();
    push('alpha__one');
    push('alpha__two');
    push('alpha__down', { failed: true });
  });

  it('reports every card, in the order the column reads', () => {
    const titles = ui.clipboardSnapshot().results.map((r) => r.title);
    assert.deepEqual(titles, ['alpha__one', 'alpha__two', 'alpha__down']);
  });

  it('carries what was asked as well as what came back', () => {
    const [first] = ui.clipboardSnapshot().results;
    assert.equal(first.method, 'tools/call');
    assert.deepEqual(first.params, { name: 'alpha__one', arguments: { n: 1 } });
    assert.deepEqual(first.raw, { content: [{ type: 'text', text: 'ok' }] });
    assert.equal(first.failed, false);
    // An ISO instant, so the document can order a session against the clock.
    assert.match(first.at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it('marks the failed card as failed', () => {
    const down = ui.clipboardSnapshot().results.find((r) => r.title === 'alpha__down');
    assert.equal(down.failed, true);
  });

  it('forgets a card the moment its trashcan is pressed', () => {
    // The column *is* the list: there is no second structure to keep in step, which is
    // exactly the bug this pins. A briefing listing a result the person deleted is worse
    // than one that is merely out of date.
    document.querySelectorAll('#results .card')[1].querySelector('.card-del').click();
    assert.deepEqual(
      ui.clipboardSnapshot().results.map((r) => r.title),
      ['alpha__one', 'alpha__down'],
    );
  });

  it('is empty once the column is cleared', () => {
    ui.clearResults();
    assert.deepEqual(ui.clipboardSnapshot().results, []);
  });
});

describe('the snapshot describes the Injectable values column', () => {
  const CONTINENTS = 'mcpgw://zoo/zoo%3A%2F%2Fcontinents';

  before(async () => {
    selectServer(ui, 'zoo', {
      resources: ['zoo://continents'],
      templates: ['zoo://continents/{continent}/animals'],
    });
    mcpAnswering(ui, {
      [CONTINENTS]: enumBody(['africa', 'asia'], { labels: ['Africa', 'Asia'] }),
    });
    ui.openVocabulary(CONTINENTS);
    await settle(ui);
  });

  it('reports the open vocabulary, where it came from, and where it is spent', () => {
    const [group] = ui.clipboardSnapshot().variables;
    assert.equal(group.variable, 'continent');
    assert.equal(group.listing, 'zoo://continents');
    assert.match(group.spends, /\{continent\}/);
    assert.deepEqual(group.values.map((v) => v.value), ['africa', 'asia']);
    assert.deepEqual(group.picked, []);
  });

  it('reports what was picked, and in which mode', () => {
    const pick = ui.pickFor(CONTINENTS, 'continent');
    pick.multi = true;
    pick.values.add('africa');
    pick.values.add('asia');
    const [group] = ui.clipboardSnapshot().variables;
    assert.equal(group.multi, true);
    assert.deepEqual(group.picked.sort(), ['africa', 'asia']);
  });

  it('publishes the two columns and nothing about the page around them', () => {
    // `clipboard.py` renders the calls and the picks; it renders neither the selected
    // server nor the bind address, and a key published here that nothing renders is dead
    // weight on a payload whose whole point is to stay small.
    assert.deepEqual(Object.keys(ui.clipboardSnapshot()).sort(), ['results', 'variables']);
  });
});

describe('the modal', () => {
  before(async () => {
    ui.clearResults();
    push('alpha__one');
    published = [];
    $('btn-clipboard').click();
    await settleClipboard();
  });

  it('opens, publishes the snapshot, and shows what the daemon rendered', () => {
    assert.equal($('clipboard-dialog').open, true);
    assert.equal(published.length, 1);
    assert.equal(published[0].results[0].title, 'alpha__one');
    // Rendered *there*, shown here. The page never writes this text itself -- that is what
    // keeps the modal and `gateway__clipboard` from drifting. See `clipboard.md`.
    assert.equal($('clip-text').value, answer);
  });

  it('says how much document there is, since the reader is about to paste it', () => {
    assert.match($('clip-note').textContent, /lines/);
  });

  it('is a plain editor: the text can simply be typed into', () => {
    const box = $('clip-text');
    assert.equal(box.tagName, 'TEXTAREA');
    assert.equal(box.readOnly, false);
  });

  it('copies what is in the box, including an edit', async () => {
    const box = $('clip-text');
    box.value = `${answer}\n\nand a note I added by hand\n`;
    box.dispatchEvent(new window.Event('input'));
    $('btn-clip-copy').click();
    await settleClipboard();
    assert.equal(clipboard.copied.at(-1), box.value);
  });

  it('leaves an edited box alone when a result lands behind it', async () => {
    const edited = $('clip-text').value;
    answer = '# a different rendering\n';
    push('alpha__two');
    await settleClipboard();
    assert.equal($('clip-text').value, edited, 'an arriving card does not discard an edit');
  });

  it('regenerates on request, throwing the edit away only when asked', async () => {
    $('btn-clip-regen').click();
    await settleClipboard();
    assert.equal($('clip-text').value, answer);
  });
});

// The toggle between the editor and the rendering. The renderer itself is pinned in
// `markdown.test.mjs`; what is worth asserting here is the one rule the two views share --
// the textarea is the document, and the rendering is only ever a window onto it.
describe('the preview toggle', () => {
  before(async () => {
    ui.clearResults();
    push('alpha__one');
    answer = '# a heading\n\nand a **paragraph**\n';
    $('btn-clipboard').click();
    await settleClipboard();
  });

  it('opens on the rendering, because reading it is what happens first', () => {
    assert.equal($('clip-preview').hidden, false);
    assert.equal($('clip-text').hidden, true);
    assert.equal($('btn-clip-preview').textContent, 'Edit');
    assert.equal($('clip-preview').querySelector('h1').textContent, 'a heading');
    assert.equal($('clip-preview').querySelector('strong').textContent, 'paragraph');
  });

  it('sits in the modal header, not among the buttons that decide what leaves', () => {
    assert.equal($('btn-clip-preview').closest('.clip-head') !== null, true);
    assert.equal($('btn-clip-preview').closest('.clip-actions'), null);
  });

  it('swaps in the editor, and says it is now the way back', () => {
    $('btn-clip-preview').click();
    assert.equal($('clip-text').hidden, false);
    assert.equal($('clip-preview').hidden, true);
    assert.equal($('btn-clip-preview').textContent, 'Preview');
  });

  it('renders what is in the box, edits included', () => {
    const box = $('clip-text');
    box.value = '## mine\n';
    box.dispatchEvent(new window.Event('input'));
    $('btn-clip-preview').click();
    assert.equal($('clip-preview').querySelector('h2').textContent, 'mine');
  });

  it('copies the text and not the rendering, while the rendering is what is on screen',
    async () => {
      assert.equal($('clip-preview').hidden, false, 'still previewing');
      $('btn-clip-copy').click();
      await settleClipboard();
      assert.equal(clipboard.copied.at(-1), '## mine\n');
    });

  it('follows a regenerate without being toggled twice', async () => {
    answer = '### regenerated\n';
    $('btn-clip-regen').click();
    await settleClipboard();
    assert.equal($('clip-preview').hidden, false);
    assert.equal($('clip-preview').querySelector('h3').textContent, 'regenerated');
  });

  it('follows a card that lands behind the modal', async () => {
    answer = '### a later rendering\n';
    push('alpha__two');
    await settleClipboard();
    assert.equal($('clip-preview').querySelector('h3').textContent, 'a later rendering');
  });

  it('remembers a switch to the editor the next time the modal opens', async () => {
    // Opening on the rendering is the default, not a mode the modal snaps back to:
    // somebody who went to the text is working on the text.
    $('btn-clip-preview').click();
    $('clipboard-dialog').close();
    $('btn-clipboard').click();
    await settleClipboard();
    assert.equal($('clip-text').hidden, false, 'the choice is a preference, not a mode');
    $('btn-clip-preview').click();
  });
});
