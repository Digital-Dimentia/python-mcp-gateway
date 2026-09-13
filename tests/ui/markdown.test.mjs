// `markdown.js`, on its own.
//
// It needs a `document` and nothing else — no page, no socket — so this file builds its own
// small jsdom rather than calling `boot()`. That is not a shortcut: the renderer having no
// dependency on the admin page is the property that lets it be read as what it is, a pure
// function from text to nodes.
//
// The assertions that matter most are the negative ones. This renderer is pointed at text
// that quotes backend output, so "an `<img onerror>` in a tool result is characters, not an
// element" is not a nicety about escaping — it is the reason the preview is allowed to
// exist at all. See `src/mcp_gateway/clipboard.md`.

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

let renderMarkdown;
let doc;

before(async () => {
  const dom = new JSDOM('<main id="out"></main>');
  globalThis.document = dom.window.document;
  doc = dom.window.document;
  ({ renderMarkdown } = await import('../../src/mcp_gateway/ui/markdown.js'));
});

/** Render `text` into a fresh box and hand the box back. */
function render(text) {
  const box = doc.createElement('div');
  box.append(renderMarkdown(text));
  return box;
}

describe('blocks', () => {
  it('renders ATX headings at their own level', () => {
    const box = render('# one\n\n## two\n\n### three\n');
    assert.deepEqual([...box.children].map((n) => n.tagName), ['H1', 'H2', 'H3']);
    assert.equal(box.querySelector('h3').textContent, 'three');
  });

  it('joins a wrapped paragraph into one sentence', () => {
    const box = render('a sentence that was\nwrapped by the writer\n');
    assert.equal(box.children.length, 1);
    assert.equal(box.textContent, 'a sentence that was wrapped by the writer');
  });

  it('gathers consecutive bullets into one list', () => {
    const box = render('- alpha\n- beta\n\nafter\n');
    const items = box.querySelectorAll('ul li');
    assert.equal(items.length, 2);
    assert.equal(items[1].textContent, 'beta');
    assert.equal(box.lastElementChild.tagName, 'P');
  });

  it('keeps a fenced payload verbatim, newlines and all', () => {
    const box = render('```json\n{\n  "a": 1\n}\n```\n');
    const code = box.querySelector('pre code');
    assert.equal(code.textContent, '{\n  "a": 1\n}');
    assert.equal(code.dataset.lang, 'json');
  });

  it('closes a four-backtick fence only on four, which is why the document opens with four',
    () => {
      // `clipboard.py` wraps payloads in four backticks precisely so a backend that answers
      // in Markdown cannot end the fence from inside it. A renderer that closed on the
      // inner three would show the rest of that payload as prose.
      const box = render('````\nbefore\n```\ninside\n```\nafter\n````\n\ntail\n');
      assert.equal(box.querySelector('pre code').textContent,
        'before\n```\ninside\n```\nafter');
      assert.equal(box.lastElementChild.textContent, 'tail');
    });

  it('runs an unclosed fence to the end rather than abandoning it', () => {
    // A truncated payload is a cut that can land mid-fence, and `MAX_PAYLOAD_CHARS` makes
    // that a thing that happens rather than a thing that might.
    const box = render('````\ncut off here\n');
    assert.equal(box.querySelector('pre code').textContent, 'cut off here');
  });
});

describe('inline spans', () => {
  it('renders code, bold and emphasis', () => {
    const box = render('a `tool__name` is **bold** and _quiet_\n');
    assert.equal(box.querySelector('code').textContent, 'tool__name');
    assert.equal(box.querySelector('strong').textContent, 'bold');
    assert.equal(box.querySelector('em').textContent, 'quiet');
  });

  it('leaves markup characters inside a code span alone', () => {
    const box = render('`**not bold**` and `_not em_`\n');
    assert.equal(box.querySelectorAll('strong').length, 0);
    assert.equal(box.querySelectorAll('em').length, 0);
    assert.equal(box.querySelectorAll('code')[0].textContent, '**not bold**');
  });

  it('does not read an intra-word underscore as emphasis', () => {
    // Tool names in this document are `server__tool`, and prose mentions them outside code
    // spans often enough. Bolding the second half of one would be a rendering nobody could
    // account for from the text.
    const box = render('the gateway__clipboard tool and zoo__feed\n');
    assert.equal(box.querySelectorAll('strong').length, 0);
    assert.equal(box.querySelectorAll('em').length, 0);
    assert.equal(box.textContent, 'the gateway__clipboard tool and zoo__feed');
  });

  it('leaves an unmatched marker as the character it is', () => {
    const box = render('2 * 3 and a _lone underscore\n');
    assert.equal(box.textContent, '2 * 3 and a _lone underscore');
  });
});

describe('nothing in the document is ever parsed as markup', () => {
  it('shows HTML in a payload as characters', () => {
    const box = render('````\n<img src=x onerror="alert(1)">\n````\n');
    assert.equal(box.querySelectorAll('img').length, 0);
    assert.equal(box.querySelector('pre code').textContent, '<img src=x onerror="alert(1)">');
  });

  it('shows HTML in prose as characters too', () => {
    const box = render('a backend said <script>alert(1)</script> to us\n');
    assert.equal(box.querySelectorAll('script').length, 0);
    assert.equal(box.textContent, 'a backend said <script>alert(1)</script> to us');
  });

  it('renders no links at all, so a javascript: URL is never clickable', () => {
    // Deliberate, not missing: the document quotes untrusted output, and the person opened
    // the preview to read their briefing rather than to be offered somewhere to go.
    const box = render('see [click me](javascript:alert(1)) for details\n');
    assert.equal(box.querySelectorAll('a').length, 0);
    assert.match(box.textContent, /\[click me\]/);
  });

  it('renders an empty document as nothing rather than throwing', () => {
    for (const empty of ['', null, undefined, '\n\n']) {
      assert.equal(render(empty).childNodes.length, 0);
    }
  });
});
