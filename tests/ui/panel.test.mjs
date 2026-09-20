// The three gates a panel passes before it reaches the socket, and the card it lands on.
//
// `panel_declarative.test.mjs` is about what a document draws. This is about what a drawn
// document is then *allowed to do*, which is the half with the sharp edges: a panel can ask
// for a tool call, and everything here is the answer to "which ones, and who said so".

import assert from 'node:assert/strict';
import test from 'node:test';

import { boot } from './harness.mjs';

const { window, document } = await boot();
const panel = await import('../../src/mcp_gateway_ui/screens/basics/panel.js');

// jsdom has `<dialog>` but no real modality; the harness already stubs `showModal`.
const dialog = () => document.querySelector('dialog.panel-consent');
const clickIn = (root, text) => {
  const button = [...root.querySelectorAll('button')].find((b) => b.textContent === text);
  assert.ok(button, `no button labelled ${text}`);
  button.click();
};

/** A page with one backend's tools listed and a socket that records rather than sends. */
function wire({ tools = [], onRequest, mint } = {}) {
  const sent = [];
  const cards = [];
  const minted = [];
  panel.install({
    mint: async (uri) => {
      minted.push(uri);
      return mint ? mint(uri) : { url: '/panel/tok' };
    },
    state: {
      listings: { tools, prompts: [], resources: [], templates: [] },
      mcp: {
        request: async (method, params) => {
          sent.push({ method, params });
          return onRequest ? onRequest(method, params) : { content: [] };
        },
      },
    },
    pushCard: (card) => cards.push(card),
  });
  return { sent, cards, minted };
}

const toolEntry = (name, extra = {}) => ({ name, inputSchema: { type: 'object' }, ...extra });
const withPanel = (name, uri) => toolEntry(name, { _meta: { ui: { resourceUri: uri } } });

const PANEL_URI = 'mcpgw://zoo/ui%3A%2F%2Fzoo%2Fboard';

const panelDoc = (blocks) => JSON.stringify({ panel: 1, blocks });

/** A `resources/read` answering with a declarative panel document. */
const readsPanel = (blocks) => (method) => {
  if (method === 'resources/read') {
    return {
      contents: [
        {
          uri: PANEL_URI,
          mimeType: 'application/json;profile=mcp-app-declarative',
          text: panelDoc(blocks),
        },
      ],
    };
  }
  return { content: [{ type: 'text', text: 'ok' }] };
};

const action = (tool) => ({ type: 'action', tool, schema: { type: 'object', properties: {} } });

/** Open a panel and hand back the button its single action rendered. */
async function openWithAction(tool, options = {}) {
  const harness = wire({
    tools: [withPanel('zoo__board', PANEL_URI), toolEntry('zoo__restart'), ...(options.tools || [])],
    onRequest: readsPanel([action(tool)]),
  });
  await panel.openPanel({ entry: withPanel('zoo__board', PANEL_URI), result: {} });
  const card = harness.cards.at(-1);
  return { ...harness, card, button: card.body.querySelector('.panel-action .btn') };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// --- panelFor ---------------------------------------------------------------------

test('a tool declares a panel by carrying a resourceUri and nothing else', () => {
  assert.equal(panel.panelFor(withPanel('zoo__board', PANEL_URI)), PANEL_URI);
  assert.equal(panel.panelFor(toolEntry('zoo__plain')), null);
  assert.equal(panel.panelFor({ _meta: { ui: { resourceUri: 42 } } }), null);
  assert.equal(panel.panelFor(undefined), null);
});

// --- opening ----------------------------------------------------------------------

test('opening a panel reads its resource and pushes one card', async () => {
  const { sent, cards } = wire({
    tools: [withPanel('zoo__board', PANEL_URI)],
    onRequest: readsPanel([{ type: 'text', text: 'drawn' }]),
  });
  await panel.openPanel({ entry: withPanel('zoo__board', PANEL_URI), result: { ok: true } });

  assert.deepEqual(sent, [{ method: 'resources/read', params: { uri: PANEL_URI } }]);
  assert.equal(cards.length, 1);
  assert.match(cards[0].body.textContent, /drawn/);
  assert.deepEqual(cards[0].raw, { ok: true }, 'the card still records the wire result');
});

test('a panel that cannot be read still shows you the result you already have', async () => {
  const { cards } = wire({
    tools: [withPanel('zoo__board', PANEL_URI)],
    onRequest: () => {
      throw new Error('nope');
    },
  });
  await panel.openPanel({
    entry: withPanel('zoo__board', PANEL_URI),
    result: { content: [{ type: 'text', text: 'the actual answer' }] },
  });
  assert.match(cards[0].body.textContent, /could not be shown/);
  assert.match(cards[0].body.textContent, /the actual answer/);
});

test('an html-tier panel is framed from a minted url, never inlined into this page', async () => {
  const { cards, minted } = wire({
    tools: [withPanel('zoo__board', PANEL_URI)],
    onRequest: () => ({
      contents: [{ uri: PANEL_URI, mimeType: 'text/html;profile=mcp-app', text: '<h1>hi</h1>' }],
    }),
  });
  await panel.openPanel({
    entry: withPanel('zoo__board', PANEL_URI),
    result: { content: [{ type: 'text', text: 'the actual answer' }] },
  });

  // The document went nowhere near this DOM: it is fetched by the frame, from an endpoint
  // that serves it under `sandbox allow-scripts`. A build that "helpfully" inlined it would
  // be running a backend's script on the origin that holds the access key.
  assert.deepEqual(minted, [PANEL_URI]);
  assert.equal(cards[0].body.querySelector('h1'), null);
  const frame = cards[0].body.querySelector('iframe');
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
});

test('when a panel offers both tiers the one that executes nothing wins', async () => {
  const { cards } = wire({
    tools: [withPanel('zoo__board', PANEL_URI)],
    onRequest: () => ({
      contents: [
        { uri: PANEL_URI, mimeType: 'text/html;profile=mcp-app', text: '<h1>hi</h1>' },
        {
          uri: PANEL_URI,
          mimeType: 'application/json;profile=mcp-app-declarative',
          text: JSON.stringify({ panel: 1, blocks: [{ type: 'text', text: 'drawn' }] }),
        },
      ],
    }),
  });
  await panel.openPanel({
    entry: withPanel('zoo__board', PANEL_URI),
    result: { ok: true },
  });

  // Picked by mimeType rather than by position -- the HTML one is first here on purpose.
  assert.match(cards[0].body.textContent, /drawn/);
  assert.equal(cards[0].body.querySelector('iframe'), null);
});

// --- which name a panel may use ----------------------------------------------------

test('a panel names its own tool, not the one the gateway prefixed', async () => {
  // The bug this pins (python-mcp-gateway-5bx): a backend writes `restart`, because the
  // prefix is a server name out of `servers.yaml` chosen long after the panel was written.
  // The host resolved that as an exact match, missed, and refused the panel's whole reason
  // for existing.
  const { button, sent } = await openWithAction('restart');
  button.click();
  await settle();
  clickIn(dialog(), 'Allow once');
  await settle();

  const call = sent.find((message) => message.method === 'tools/call');
  assert.equal(call.params.name, 'zoo__restart', 'the socket carries the listed name');
});

test('the consent dialog names the call it is authorising, not the one that was asked for', async () => {
  const { button } = await openWithAction('restart');
  button.click();
  await settle();

  // A prompt saying `restart` over a socket carrying `zoo__restart` is a prompt about a
  // different call from the one it grants.
  assert.match(dialog().textContent, /zoo__restart/);
  clickIn(dialog(), 'Deny');
});

test('the public spelling still works, because a panel may echo back what it was told', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  button.click();
  await settle();
  clickIn(dialog(), 'Allow once');
  await settle();

  assert.equal(sent.find((m) => m.method === 'tools/call').params.name, 'zoo__restart');
});

test('one grant covers a tool however the panel spells it', async () => {
  const harness = wire({
    tools: [withPanel('zoo__board', PANEL_URI), toolEntry('zoo__restart')],
    onRequest: readsPanel([action('restart'), action('zoo__restart')]),
  });
  await panel.openPanel({ entry: withPanel('zoo__board', PANEL_URI), result: {} });
  const buttons = harness.cards.at(-1).body.querySelectorAll('.panel-action .btn');

  buttons[0].click();
  await settle();
  clickIn(dialog(), 'Allow for this panel');
  await settle();

  // The second block spells the same tool the other way. A ledger keyed on what the panel
  // said rather than on what it resolved to would ask again -- or, worse, let a second
  // grant be spent under a name the person never saw.
  buttons[1].click();
  await settle();
  assert.equal(dialog(), null, 'the grant is against the tool, not the spelling');
});

// --- gate 1: scope ----------------------------------------------------------------

test('a local name is still confined to this panel’s backend', async () => {
  // `other` has a `restart` too. Resolving the panel's own spelling must not reach it.
  const { button, sent } = await openWithAction('wipe', {
    tools: [toolEntry('other__wipe')],
  });
  const before = sent.length;
  button.click();
  await settle();
  assert.equal(sent.length, before, 'nothing may reach the socket');
  assert.equal(dialog(), null, 'and it must not even ask');
});

test('a panel may not call another backend’s tool', async () => {
  const { button, sent } = await openWithAction('other__wipe', {
    tools: [toolEntry('other__wipe')],
  });
  const before = sent.length;
  button.click();
  await settle();
  assert.equal(sent.length, before, 'nothing may reach the socket');
  assert.equal(dialog(), null, 'and it must not even ask');
});

test('a panel may not call the gateway’s own tools', async () => {
  const { button, sent } = await openWithAction('gateway__reload_config', {
    tools: [toolEntry('gateway__reload_config')],
  });
  const before = sent.length;
  button.click();
  await settle();
  assert.equal(sent.length, before);
  assert.equal(dialog(), null);
});

test('a panel may not call a tool that is not listed at all', async () => {
  const { button, sent } = await openWithAction('zoo__imaginary');
  const before = sent.length;
  button.click();
  await settle();
  assert.equal(sent.length, before);
});

// --- gate 2: consent --------------------------------------------------------------

test('nothing reaches the socket until consent is answered', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  const before = sent.length;
  button.click();
  await settle();

  assert.ok(dialog(), 'the first call must ask');
  assert.equal(sent.length, before, 'and send nothing while it is asking');

  clickIn(dialog(), 'Allow once');
  await settle();
  assert.equal(sent.length, before + 1);
  assert.equal(sent.at(-1).method, 'tools/call');
  assert.equal(sent.at(-1).params.name, 'zoo__restart');
});

test('denying sends nothing and says so on the action', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  const before = sent.length;
  button.click();
  await settle();
  clickIn(dialog(), 'Deny');
  await settle();

  assert.equal(sent.length, before, 'a refusal is a refusal');
  assert.match(button.closest('.panel-action').textContent, /Refused/);
});

test('dismissing the dialog is a refusal, not a grant', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  const before = sent.length;
  button.click();
  await settle();
  dialog().dispatchEvent(new window.Event('cancel'));
  await settle();
  assert.equal(sent.length, before, 'walking away must never read as yes');
});

test('allow once asks again the next time', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  button.click();
  await settle();
  clickIn(dialog(), 'Allow once');
  await settle();
  const after = sent.length;

  await new Promise((resolve) => setTimeout(resolve, panel.MIN_GAP_MS + 10));
  button.click();
  await settle();
  assert.ok(dialog(), 'once means once');
  clickIn(dialog(), 'Deny');
  await settle();
  assert.equal(sent.length, after);
});

test('allow for this panel does not ask again', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  button.click();
  await settle();
  clickIn(dialog(), 'Allow for this panel');
  await settle();
  const after = sent.length;

  await new Promise((resolve) => setTimeout(resolve, panel.MIN_GAP_MS + 10));
  button.click();
  await settle();
  assert.equal(dialog(), null, 'a standing grant is standing');
  assert.equal(sent.length, after + 1);
});

test('a grant belongs to one card, so a second panel asks for itself', async () => {
  const first = await openWithAction('zoo__restart');
  first.button.click();
  await settle();
  clickIn(dialog(), 'Allow for this panel');
  await settle();

  const second = await openWithAction('zoo__restart');
  second.button.click();
  await settle();
  assert.ok(dialog(), 'a new card is a new ledger');
  clickIn(dialog(), 'Deny');
  await settle();
});

// --- gate 3: budget ---------------------------------------------------------------

test('two calls in quick succession are refused rather than queued', async () => {
  const { button, sent } = await openWithAction('zoo__restart');
  button.click();
  await settle();
  clickIn(dialog(), 'Allow for this panel');
  await settle();
  const after = sent.length;

  button.click();
  await settle();
  assert.equal(sent.length, after, 'the gap is a gap');
  assert.match(button.closest('.panel-action').textContent, /moment/);
});

// --- what a call looks like afterwards ---------------------------------------------

test('a panel-originated call lands as its own card, marked as from a panel', async () => {
  const { button, cards } = await openWithAction('zoo__restart');
  const before = cards.length;
  button.click();
  await settle();
  clickIn(dialog(), 'Allow once');
  await settle();

  assert.equal(cards.length, before + 1, 'a panel cannot make a call you do not see');
  const card = cards.at(-1);
  assert.equal(card.title, 'zoo__restart');
  assert.match(card.subtitle, /panel/);
  assert.equal(card.request.method, 'tools/call');
});
