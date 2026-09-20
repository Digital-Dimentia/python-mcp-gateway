// SEP-1865's HTML tier, from this side of the frame.
//
// jsdom will not load a `/panel/<token>` document -- there is no daemon behind this DOM --
// so what is driven here is the bridge, with a real `MessageChannel` standing in for the
// one a framed panel would be handed. That is not a weaker test than it looks: the port is
// the entire interface, and every rule worth keeping is a rule about what crosses it.
//
// The load-bearing one is that `event.origin` is never consulted. A sandboxed document has
// an opaque origin, which serialises as the string `"null"` and is shared by every sandboxed
// frame on the page -- so an origin check here proves nothing and, written the obvious way,
// fails closed and breaks the handshake. Identity is the port.

import assert from 'node:assert/strict';
import test, { after } from 'node:test';

import { boot } from './harness.mjs';

const { window, document } = await boot();
const {
  bridge,
  renderHtmlPanel,
  isHtmlPanel,
  DEFAULT_HEIGHT,
  MAX_HEIGHT,
  MIN_HEIGHT,
} = await import('../../src/mcp_gateway_ui/panel_html.js');

// jsdom implements no `MessageChannel`, so the one in scope here is Node's -- which is the
// same object shape, structured clone included. The module under test reaches for the global
// the same way a page does; in a browser that global is the window's.

/** A panel's end of the channel, recording what the host sends it. */
function panelEnd(channel) {
  const seen = [];
  channel.port2.onmessage = (event) => seen.push(event.data);
  channel.port2.start?.();
  return seen;
}

// A port message is delivered on the event loop rather than in a microtask, and an answer
// is often one turn behind the request that provoked it. Three turns rather than one, so a
// test asserts about the bridge and never about how fast Node drained a queue.
const settle = async () => {
  for (let turn = 0; turn < 3; turn += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

//: Every channel a test opened. An open `MessagePort` holds the event loop, so without this
//: the suite passes and then never exits -- which reads as a hung test run, not a leak.
const opened = [];
after(() => {
  for (const channel of opened) {
    channel.port1.close();
    channel.port2.close();
  }
});

function wire({ onAction = async () => ({ content: [] }), result = { ok: true } } = {}) {
  const channel = new MessageChannel();
  opened.push(channel);
  const seen = panelEnd(channel);
  const resized = [];
  const notes = [];
  const link = bridge({
    port: channel.port1,
    call: { name: 'zoo__board', arguments: { id: 'alpha' } },
    result,
    onAction,
    onResize: (height) => resized.push(height),
    onNote: (text) => notes.push(text),
  });
  const send = (message) => channel.port2.postMessage(message);
  return { seen, resized, notes, send, link, channel };
}

const named = (seen, method) => seen.filter((message) => message.method === method);
const answerTo = (seen, id) => seen.find((message) => message.id === id);

// --- the tier's own mimeType ----------------------------------------------------

test('the HTML tier is recognised by its profile parameter, not by text/html alone', () => {
  assert.equal(isHtmlPanel('text/html;profile=mcp-app'), true);
  assert.equal(isHtmlPanel('text/html; profile="mcp-app"'), true);
  assert.equal(isHtmlPanel('text/html'), false);
  assert.equal(isHtmlPanel('text/plain;profile=mcp-app'), false);
  assert.equal(isHtmlPanel('application/json;profile=mcp-app-declarative'), false);
  assert.equal(isHtmlPanel(undefined), false);
});

// --- the handshake --------------------------------------------------------------

test('a panel that initialises is told what it is talking to and what it may ask for', async () => {
  const { seen, send } = wire();
  send({ jsonrpc: '2.0', id: 1, method: 'ui/initialize', params: {} });
  await settle();

  const reply = answerTo(seen, 1);
  assert.ok(reply.result.protocolVersion);
  assert.equal(reply.result.capabilities.tools.call, true);
  // Absent rather than false: a panel has no route to these at all.
  assert.equal(reply.result.capabilities.resources, undefined);
  assert.equal(reply.result.capabilities.sampling, undefined);
});

test('the tool input and result arrive only once the panel says it is ready', async () => {
  const { seen, send } = wire({ result: { structuredContent: { machines: [] } } });

  send({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' });
  await settle();
  assert.equal(named(seen, 'ui/notifications/tool-result').length, 0);

  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
  await settle();

  const input = named(seen, 'ui/notifications/tool-input')[0];
  assert.equal(input.params.name, 'zoo__board');
  assert.deepEqual(input.params.arguments, { id: 'alpha' });
  assert.deepEqual(named(seen, 'ui/notifications/tool-result')[0].params.result, {
    structuredContent: { machines: [] },
  });
});

test('a second initialized notification does not deliver the result twice', async () => {
  const { seen, send } = wire();
  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
  await settle();

  assert.equal(named(seen, 'ui/notifications/tool-result').length, 1);
});

// --- the one door ---------------------------------------------------------------

test('a tool call from a panel goes through the gates and its answer comes back', async () => {
  const asked = [];
  const { seen, send } = wire({
    onAction: async (request) => {
      asked.push(request);
      return { content: [{ type: 'text', text: 'done' }] };
    },
  });

  send({ jsonrpc: '2.0', id: 7, method: 'tools/call', params: { name: 'zoo__restart', arguments: { id: 'beta' } } });
  await settle();

  assert.deepEqual(asked, [{ tool: 'zoo__restart', arguments: { id: 'beta' } }]);
  assert.deepEqual(answerTo(seen, 7).result.content[0].text, 'done');
});

test('a refused call reaches the panel as an error and the page as a note', async () => {
  const { seen, notes, send } = wire({
    onAction: async () => { throw new Error('Refused.'); },
  });

  send({ jsonrpc: '2.0', id: 8, method: 'tools/call', params: { name: 'other__tool' } });
  await settle();

  assert.equal(answerTo(seen, 8).error.message, 'Refused.');
  assert.deepEqual(notes, ['Refused.']);
});

test('a tools/call sent as a notification asks for nothing, because nothing can answer it', async () => {
  const asked = [];
  const { send } = wire({ onAction: async (request) => { asked.push(request); return {}; } });

  send({ jsonrpc: '2.0', method: 'tools/call', params: { name: 'zoo__restart' } });
  await settle();

  assert.deepEqual(asked, []);
});

test('a method this host does not have is a -32601 rather than silence', async () => {
  const { seen, send } = wire();
  send({ jsonrpc: '2.0', id: 9, method: 'resources/read', params: { uri: 'ui://x' } });
  await settle();

  assert.equal(answerTo(seen, 9).error.code, -32601);
});

// --- presentation ---------------------------------------------------------------

test('a size a panel asks for is clamped rather than taken', async () => {
  const { resized, send } = wire();
  send({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 500 } });
  send({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 99999 } });
  send({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: -4 } });
  send({ jsonrpc: '2.0', method: 'ui/notifications/size-changed', params: { height: 'tall' } });
  await settle();

  assert.deepEqual(resized, [500, MAX_HEIGHT, MIN_HEIGHT]);
});

test('a theme change reaches an initialised panel and not one that never said hello', async () => {
  const quiet = wire();
  document.documentElement.setAttribute('data-theme', 'dark');
  await settle();
  assert.equal(named(quiet.seen, 'ui/notifications/host-context-changed').length, 0);

  const { seen, send } = wire();
  send({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
  await settle();
  document.documentElement.setAttribute('data-theme', 'light');
  await settle();

  const changed = named(seen, 'ui/notifications/host-context-changed');
  assert.equal(changed.at(-1).params.theme, 'light');
  document.documentElement.removeAttribute('data-theme');
});

test('a closed bridge stops answering', async () => {
  const { seen, send, link } = wire();
  link.close();
  send({ jsonrpc: '2.0', id: 1, method: 'ui/initialize' });
  await settle();

  assert.deepEqual(seen, []);
});

// --- the frame ------------------------------------------------------------------

test('the frame is sandboxed without allow-same-origin, whatever the header says', async () => {
  const body = renderHtmlPanel({
    reference: 'mcpgw://zoo/ui%3A%2F%2Fzoo%2Fpanel',
    call: { name: 'zoo__board', arguments: {} },
    result: {},
    onAction: async () => ({}),
    mint: async () => ({ url: '/panel/tok' }),
  });
  const frame = body.querySelector('iframe');

  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
  assert.equal(frame.getAttribute('height'), String(DEFAULT_HEIGHT));
  await settle();
  assert.equal(frame.getAttribute('src'), '/panel/tok');
});

test('a mint that fails leaves a note rather than an empty frame and no explanation', async () => {
  const body = renderHtmlPanel({
    reference: 'mcpgw://zoo/x',
    call: { name: 'zoo__board', arguments: {} },
    result: {},
    onAction: async () => ({}),
    mint: async () => { throw new Error('Unauthorized'); },
  });
  await settle();

  assert.match(body.querySelector('.note').textContent, /Unauthorized/);
  assert.equal(body.querySelector('iframe').getAttribute('src'), null);
});
