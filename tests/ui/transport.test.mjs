// `rpc.js` driven through frames in both directions, with no server and no browser.
//
// This suite is what the transport seam bought. Before it, the only way to keep these
// classes from dialling out was to write a stub over `globalThis.WebSocket` -- which could
// silence a socket but never drive one, because nothing ever called the handlers back. So
// every line below (the handshake, the pending table, the backoff, the cancellation that
// follows a timeout) was untestable, and the two of them that are protocol contracts with
// `session.py` were being taken on faith.
//
// No jsdom here, deliberately. `RpcSocket` touches `EventTarget`, `CustomEvent` and
// `setTimeout` and nothing else -- a DOM would only slow the file down and invite someone
// to reach for one.

import { describe, it, mock } from 'node:test';
import assert from 'node:assert/strict';

import {
  AdminSocket, McpSocket, RpcError, PROTOCOL_VERSION, PANEL_MIME_TYPES, setTransport,
} from '../../src/mcp_gateway_ui/rpc.js';

/** The documented schedule in `rpc.js`. Spelled again here so a change has to be meant. */
const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000];

/**
 * A transport a test drives.
 *
 * The narrow half of the `WebSocket` interface -- `send`, `close`, `readyState`, four
 * handlers -- plus `open`/`deliver`/`drop`, which are the far side of the wire.
 */
class Wire {
  static opened = [];

  constructor(path, key) {
    this.path = path;
    this.key = key;
    this.readyState = 0;                 // CONNECTING
    this.sent = [];
    this.closedByClient = false;
    Wire.opened.push(this);
  }

  static reset() {
    Wire.opened = [];
    setTransport((path, key) => new Wire(path, key));
  }

  static get latest() { return Wire.opened[Wire.opened.length - 1]; }

  // ── the RpcSocket side ───────────────────────────────────────────────────────
  send(text) { this.sent.push(text); }

  close() {
    this.closedByClient = true;
    this.readyState = 3;
  }

  // ── the far side ─────────────────────────────────────────────────────────────
  open() { this.readyState = 1; this.onopen(); }

  deliver(message) { this.onmessage({ data: JSON.stringify(message) }); }

  drop(reason = '') { this.readyState = 3; this.onclose({ reason }); }

  /** Everything written, parsed. */
  get frames() { return this.sent.map((text) => JSON.parse(text)); }

  get last() { return this.frames[this.frames.length - 1]; }
}

/** Let every already-resolved promise in the chain run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

/** An `AdminSocket` that has connected and opened. */
function connectedAdmin(key = null) {
  Wire.reset();
  const socket = new AdminSocket(key);
  socket.connect();
  Wire.latest.open();
  return socket;
}

describe('opening', () => {
  it('hands the transport the path and the key, and nothing else', () => {
    Wire.reset();
    new AdminSocket('s3cret').connect();

    assert.equal(Wire.opened.length, 1);
    assert.equal(Wire.latest.path, '/admin');
    assert.equal(Wire.latest.key, 's3cret');
  });

  it('carries no key when there is none, rather than an empty one', () => {
    Wire.reset();
    new AdminSocket(null).connect();
    assert.equal(Wire.latest.key, null);

    // `''` is not a key. The constructor coerces it, so a host that has nothing to say
    // cannot accidentally say `?key=`.
    Wire.reset();
    new AdminSocket('').connect();
    assert.equal(Wire.latest.key, null);
  });

  it('reports a transport that throws through the ordinary failure path', () => {
    setTransport(() => { throw new Error('no such host'); });
    const socket = new AdminSocket(null);
    const seen = [];
    socket.addEventListener('state', (e) => seen.push(e.detail));
    socket.connect();
    socket.close();                       // stop the retry this scheduled

    assert.deepEqual(seen.map((d) => d.state), ['connecting', 'closed']);
    assert.match(seen[1].reason, /no such host/);
  });

  it('is `ready` the moment `/admin` opens, because it has no handshake', () => {
    const socket = connectedAdmin();
    assert.equal(socket.state, 'ready');
  });
});

describe('requests', () => {
  it('writes one JSON-RPC frame and resolves on the matching id', async () => {
    const socket = connectedAdmin();
    const answer = socket.request('admin.status', {});

    assert.deepEqual(Wire.latest.last, {
      jsonrpc: '2.0', id: 1, method: 'admin.status', params: {},
    });

    Wire.latest.deliver({ jsonrpc: '2.0', id: 1, result: { name: 'gateway' } });
    assert.deepEqual(await answer, { name: 'gateway' });
  });

  it('rejects with an RpcError carrying the code the far side sent', async () => {
    const socket = connectedAdmin();
    const answer = socket.request('admin.nope', {});
    Wire.latest.deliver({
      jsonrpc: '2.0', id: 1, error: { code: -32601, message: 'no such method' },
    });

    const err = await answer.catch((e) => e);
    assert.ok(err instanceof RpcError);
    assert.equal(err.code, -32601);
    assert.equal(err.message, 'no such method');
  });

  it('rejects rather than queueing when the socket is not open', async () => {
    Wire.reset();
    const socket = new AdminSocket(null);
    socket.connect();                     // connecting, never opened

    const err = await socket.request('admin.status', {}).catch((e) => e);
    assert.equal(err.code, -32000);
    assert.equal(err.message, 'not connected');
    assert.deepEqual(Wire.latest.sent, []);
  });

  it('cancels a request that times out, so the far side stops working on it', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const socket = connectedAdmin();
    const answer = socket.request('admin.slow', {}, { timeoutMs: 5000 });

    t.mock.timers.tick(5000);
    const err = await answer.catch((e) => e);

    assert.match(err.message, /no answer in 5s/);
    // MCP's own cancellation, which the gateway propagates down to the backend's wire.
    assert.deepEqual(Wire.latest.last, {
      jsonrpc: '2.0',
      method: 'notifications/cancelled',
      params: { requestId: 1, reason: 'client timeout' },
    });
  });

  it('answers a server->client request instead of leaving it hanging', () => {
    const socket = connectedAdmin();
    // The UI declares no capabilities, so a well-behaved gateway never sends one of these.
    // An unanswered request would leave the gateway holding a future until its timeout.
    Wire.latest.deliver({ jsonrpc: '2.0', id: 99, method: 'sampling/createMessage' });

    assert.equal(Wire.latest.last.id, 99);
    assert.equal(Wire.latest.last.error.code, -32601);
  });

  it('ignores a response to an id it is not waiting on', () => {
    const socket = connectedAdmin();
    assert.doesNotThrow(() => Wire.latest.deliver({ jsonrpc: '2.0', id: 42, result: {} }));
  });

  it('ignores a frame that is not JSON rather than dying on it', () => {
    connectedAdmin();
    assert.doesNotThrow(() => Wire.latest.onmessage({ data: 'not json' }));
  });
});

describe('losing the connection', () => {
  it('rejects every pending request exactly once', async () => {
    const socket = connectedAdmin();
    const first = socket.request('admin.status', {});
    const second = socket.request('admin.backends', {});

    Wire.latest.drop();

    for (const answer of [first, second]) {
      const err = await answer.catch((e) => e);
      assert.equal(err.message, 'connection closed');
    }
    assert.equal(socket.pending.size, 0);
    socket.close();
  });

  it('distinguishes a socket that never opened from one that did', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });

    Wire.reset();
    const never = new AdminSocket(null);
    const neverSaw = [];
    never.addEventListener('state', (e) => neverSaw.push(e.detail.reason));
    never.connect();
    Wire.latest.drop();
    assert.equal(neverSaw.at(-1), 'could not connect');
    never.close();

    const once = connectedAdmin();
    const onceSaw = [];
    once.addEventListener('state', (e) => onceSaw.push(e.detail.reason));
    Wire.latest.drop();
    assert.equal(onceSaw.at(-1), 'connection closed');
    once.close();
  });

  it('passes the far side\'s reason through, which is what the shell relies on', () => {
    const socket = connectedAdmin();
    const seen = [];
    socket.addEventListener('state', (e) => seen.push(e.detail.reason));

    // A browser never supplies one -- it tells a page nothing about why a socket failed.
    // The desktop shell does: Rust sees the HTTP status, so `401` can reach the pill.
    Wire.latest.drop('401 Unauthorized');
    assert.equal(seen.at(-1), '401 Unauthorized');
    socket.close();
  });

  it('reconnects on the documented backoff, and resets it on a success', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const socket = connectedAdmin();

    for (const [index, delay] of BACKOFF_MS.entries()) {
      Wire.latest.drop();
      assert.equal(Wire.opened.length, index + 1, 'no reconnect before the delay elapses');
      t.mock.timers.tick(delay - 1);
      assert.equal(Wire.opened.length, index + 1);
      t.mock.timers.tick(1);
      assert.equal(Wire.opened.length, index + 2);
    }

    // The last step is the cap, not another doubling.
    Wire.latest.drop();
    t.mock.timers.tick(BACKOFF_MS.at(-1));
    const attemptsSoFar = Wire.opened.length;

    Wire.latest.open();
    assert.equal(socket.attempt, 0, 'an open resets the schedule');
    Wire.latest.drop();
    t.mock.timers.tick(BACKOFF_MS[0]);
    assert.equal(Wire.opened.length, attemptsSoFar + 1, 'back to the first delay');

    socket.close();
  });

  it('stops for good once closed, which is what the browser gate needs', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const socket = connectedAdmin();

    socket.close();
    assert.ok(Wire.latest.closedByClient);

    Wire.latest.drop();
    t.mock.timers.tick(60_000);
    assert.equal(Wire.opened.length, 1, 'a closed socket does not come back on its own');
  });
});

describe('the MCP handshake', () => {
  it('initializes, then announces, then is ready -- in that order', async () => {
    Wire.reset();
    const socket = new McpSocket(null);
    const states = [];
    socket.addEventListener('state', (e) => states.push(e.detail.state));
    socket.connect();
    Wire.latest.open();

    // `session.py` refuses every other method until this pair has been sent.
    const init = Wire.latest.last;
    assert.equal(init.method, 'initialize');
    assert.equal(init.params.protocolVersion, PROTOCOL_VERSION);
    // Declared honestly: claiming `sampling` here would make a backend ask this page to
    // run a model. MCP Apps is the one thing here, and it is here because it is true --
    // the gateway declares the union of its clients' lists downward, so a type in it that
    // this page cannot draw is a backend shipping a panel to a blank card.
    assert.deepEqual(init.params.capabilities, {
      extensions: { 'io.modelcontextprotocol/ui': { mimeTypes: PANEL_MIME_TYPES } },
    });
    assert.equal(socket.state, 'open', 'not ready until the handshake finishes');

    Wire.latest.deliver({
      jsonrpc: '2.0',
      id: init.id,
      result: { serverInfo: { name: 'mcp-gateway' }, capabilities: { tools: {} } },
    });
    await settle();

    assert.equal(Wire.latest.last.method, 'notifications/initialized');
    assert.deepEqual(states, ['connecting', 'open', 'ready']);
    assert.deepEqual(socket.serverInfo, { name: 'mcp-gateway' });
    assert.deepEqual(socket.capabilities, { tools: {} });
  });

  it('closes the socket when initialize is refused, rather than going ready', async () => {
    Wire.reset();
    const socket = new McpSocket(null);
    socket.connect();
    Wire.latest.open();

    Wire.latest.deliver({
      jsonrpc: '2.0',
      id: Wire.latest.last.id,
      error: { code: -32602, message: 'unsupported protocol version' },
    });
    await settle();

    assert.equal(socket.state, 'closed');
    assert.ok(Wire.latest.closedByClient);
    socket.close();
  });
});

describe('the default transport', () => {
  it('is restored by passing nothing, so a suite cannot leak its fake', (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    setTransport(null);

    const socket = new AdminSocket(null);
    const seen = [];
    socket.addEventListener('state', (e) => seen.push(e.detail));
    socket.connect();
    socket.close();

    // There is no `location` in this process, so the browser transport cannot run here --
    // which is itself the assertion. The default is the browser one, and it is not the
    // fake the rest of this file installed. A transport that throws is reported through
    // the ordinary failure path rather than thrown at the caller, so this is a `closed`
    // state carrying the reason, not an exception.
    assert.equal(seen.at(-1).state, 'closed');
    assert.match(seen.at(-1).reason, /location/);
  });
});
