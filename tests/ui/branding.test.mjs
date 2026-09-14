// White labelling, as the page applies it.
//
// The daemon's half is pinned in `tests/test_branding.py`; this is the other end of the
// same wire. What matters here is the falling-back: `applyBranding` runs on every
// `admin.status`, most of which carry no branding at all, and a gateway that configures no
// icon has to come out of it wearing the stock mark -- byte-identical to the markup that
// shipped -- rather than wearing nothing. A rebrand that cannot be *undone* by editing the
// catalogue back is a rebrand nobody can experiment with.

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

const LOGO = 'data:image/svg+xml;base64,PHN2Zy8+';

//: The shipped mark, as `index.html` names it. An asset rather than a `data:` URI: the
//: stock logo is a file to swap, and only a *configured* one has to travel inline.
const STOCK = 'logo.svg';

describe('branding', () => {
  let ui;
  let document;

  before(async () => {
    ({ ui, document } = await boot());
  });

  it('ships wearing the stock mark, before any socket has answered', () => {
    // The markup, not the code: the header is complete on the first paint rather than
    // assembling itself a round trip later.
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), STOCK);
    assert.equal(document.getElementById('brand-favicon').getAttribute('href'), STOCK);
  });

  it('leaves an unbranded gateway exactly as the markup shipped', () => {
    ui.applyBranding(undefined);
    assert.equal(document.getElementById('brand-title').textContent, 'MCP Gateway');
    assert.equal(document.title, 'MCP Gateway');
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), STOCK);
    assert.equal(document.getElementById('brand-favicon').getAttribute('href'), STOCK);
  });

  it('wears the configured title in the header and the tab', () => {
    ui.applyBranding({ title: 'Acme Internal Tools', name: 'acme-tools', icon: null });
    assert.equal(document.getElementById('brand-title').textContent, 'Acme Internal Tools');
    assert.equal(document.title, 'Acme Internal Tools');
    // A name without an icon keeps the stock mark: renaming the deployment is not a claim
    // about its logo, and a header with a hole in it would look like a failed load.
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), STOCK);
  });

  it('puts a configured mark in the header and the favicon, from the one data URI', () => {
    ui.applyBranding({ title: 'Acme', name: 'acme', icon: LOGO });
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), LOGO);
    assert.equal(document.getElementById('brand-favicon').getAttribute('href'), LOGO);
  });

  it('goes back to stock when a reload removes the block', () => {
    // The reason this is a test and not an obvious consequence: the header is rebuilt from
    // a payload that says nothing about the icon, so "no icon" has to mean "the shipped
    // one" rather than "whatever was there last time".
    ui.applyBranding({ title: 'Acme', name: 'acme', icon: LOGO });
    ui.applyBranding({ title: 'MCP Gateway', name: 'mcp-gateway', icon: null });
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), STOCK);
    assert.equal(document.getElementById('brand-favicon').getAttribute('href'), STOCK);
    assert.equal(document.title, 'MCP Gateway');
  });

  it('survives a status payload that has no branding key at all', () => {
    // An older daemon behind a newer page. It reports no `branding`, and the page must
    // render rather than throw on `undefined.title`.
    ui.applyBranding({ title: 'Acme', name: 'acme', icon: LOGO });
    ui.applyBranding(null);
    assert.equal(document.title, 'MCP Gateway');
    assert.equal(document.getElementById('brand-icon').getAttribute('src'), STOCK);
  });
});
