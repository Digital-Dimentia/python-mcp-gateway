// The page as the desktop shell runs it: no key, no gate field, no socket of its own.
//
// Its own file because `boot()` is once per process -- see `harness.mjs`. What is asserted
// here is mostly *absence*: that the window asks the host to open two named sockets and
// nothing else, that no secret is passed or stored, and that the browser-only key handling
// stands down. Absence is the whole design, so absence is what the tests are about.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

let ui;
let document;
let host;
let window;

before(async () => {
  ({ ui, document, host, window } = await boot({ shell: true }));
});

// Both sockets reconnect on their own and every request carries a 120s timeout, so a suite
// that just ends leaves real timers behind and the process sits there until the last one
// fires. Closing them is what `app.js` never does, because a window is not a test.
after(() => {
  ui.state.admin.close();
  ui.state.mcp.close();
});

describe('the window holds no secret', () => {
  it('does not look for a key, and does not find one', () => {
    assert.equal(ui.state.key, null);
  });

  it('passes no key to the host when it opens a socket', () => {
    for (const { args } of host.opened()) {
      assert.deepEqual(Object.keys(args).sort(), ['id', 'onFrame', 'path']);
      assert.ok(!JSON.stringify({ id: args.id, path: args.path }).match(/key/i));
    }
  });

  it('writes nothing to localStorage, even though the browser build does', () => {
    // Same origin, potentially the same profile: the browser build persists the key under
    // `mcp-gateway-ui.key`, and the shell must never put one there.
    assert.equal(window.localStorage.getItem('mcp-gateway-ui.key'), null);
  });
});

describe('the sockets', () => {
  it('asks the host for exactly the two JSON-RPC paths', () => {
    const paths = host.opened().map((c) => c.args.path).sort();
    assert.deepEqual(paths, ['/admin', '/mcp']);
  });

  it('mints its own connection id rather than waiting for one', () => {
    // `gw_open` is a promise and the host can push `open` down the channel before it
    // settles. `McpSocket` sends `initialize` the instant it sees `open`, so an id that
    // arrived later would drop the first frame of the handshake.
    for (const { args } of host.opened()) {
      assert.match(args.id, /^[0-9a-f-]{36}$/);
    }
    const ids = host.opened().map((c) => c.args.id);
    assert.equal(new Set(ids).size, ids.length, 'one id per socket');
  });

  it('never constructs a WebSocket of its own', () => {
    // The Tauri window's CSP names `ipc:` in `connect-src` and nothing else, so a page that
    // tried would be blocked at runtime. This is the same statement, checked earlier.
    assert.deepEqual(
      host.calls.map((c) => c.command).filter((c) => !c.startsWith('gw_')),
      [],
    );
  });

  it('speaks MCP over the host, byte for byte', async () => {
    const mcp = host.opened().findIndex((c) => c.args.path === '/mcp');
    host.deliver(mcp, { kind: 'open' });
    await new Promise((resolve) => setImmediate(resolve));

    // What the host is asked to send is a JSON-RPC frame and nothing wrapped around it:
    // the proxy relays opaque text, which is what keeps the middle column's claim true.
    const sent = host.calls.filter((c) => c.command === 'gw_send');
    assert.ok(sent.length >= 1, 'the handshake should have started');
    const first = JSON.parse(sent[0].args.text);
    assert.equal(first.method, 'initialize');
    assert.equal(first.jsonrpc, '2.0');

    // Answer it. Not politeness -- an unanswered request sits on its own 120s timeout and
    // holds the event loop open until it fires, which is a two-minute test run.
    host.deliver(mcp, {
      kind: 'frame',
      text: JSON.stringify({
        jsonrpc: '2.0',
        id: first.id,
        result: { serverInfo: { name: 'mcp-gateway' }, capabilities: {} },
      }),
    });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(ui.state.mcp.state, 'ready');

    // `session.py` refuses every other method until this notification has been sent -- so
    // it must be there, and it must come before the listing calls `app.js` fires the moment
    // the socket goes ready.
    const methods = host.calls
      .filter((c) => c.command === 'gw_send')
      .map((c) => JSON.parse(c.args.text).method);
    assert.ok(methods.includes('notifications/initialized'), methods.join(', '));
    assert.ok(
      methods.indexOf('notifications/initialized') < methods.indexOf('tools/list'),
      methods.join(', '),
    );
  });
});

describe('the gate', () => {
  it('is a splash while the gateway is still starting, not an accusation', () => {
    // The bug this pins. A cold interpreter takes a second to import, so the shell's first
    // `gw_open` answers "not listening yet" and the socket closes -- before anything has
    // gone wrong. That used to paint "This gateway may require an access key" over a window
    // that was merely still starting, and then clear it a moment later, so every launch
    // looked like a failure that fixed itself.
    ui.state.admin.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'closed', reason: 'refused' } }),
    );
    const gate = document.getElementById('gate');
    assert.equal(gate.hidden, false, 'a closed socket should still surface something');
    assert.equal(gate.dataset.mode, 'connecting');
    assert.equal(document.getElementById('gate-title').textContent, 'Starting…');
    assert.doesNotMatch(document.getElementById('gate-message').textContent, /key/i);
  });

  it('never offers a key field, because there is no key to ask a human for', () => {
    ui.state.admin.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'closed', reason: '401' } }),
    );
    assert.equal(document.getElementById('gate-key-field').hidden, true);
  });

  it('does not let a late socket close undo what the host said', () => {
    // Two writers, one panel. The host knows the gateway failed to start; a socket closing
    // afterwards knows only that it closed. Ranking the modes is what stops the second from
    // painting a hopeful splash over the first.
    host.emit('gateway-state', {
      state: 'failed', attempt: 0, reason: 'servers.yaml: unknown key `comand`', log: [],
    });
    assert.equal(document.getElementById('gate').dataset.mode, 'failed');
    ui.state.admin.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'closed', reason: 'refused' } }),
    );
    assert.equal(document.getElementById('gate').dataset.mode, 'failed');
    assert.match(document.getElementById('gate-message').textContent, /unknown key/);
  });

  it('goes away once a socket is up', () => {
    ui.state.admin.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'ready' } }),
    );
    assert.equal(document.getElementById('gate').hidden, true);
  });

  it('shows what the host says, which a browser could never know', () => {
    host.emit('gateway-state', {
      state: 'restarting', attempt: 3, reason: null, log: ['backend github exited'],
    });
    assert.match(document.getElementById('gate-message').textContent, /restarting \(attempt 3\)/);

    host.emit('gateway-state', {
      state: 'failed', attempt: 0, reason: 'servers.yaml: unknown key `comand`', log: [],
    });
    assert.match(document.getElementById('gate-message').textContent, /unknown key/);
  });

  it('keeps retrying, unlike the browser build', async () => {
    // The three-strikes stop exists so a page parked on a wrong key is not a background 401
    // generator. There is no wrong key here, and a closed socket means the host is
    // restarting the gateway -- which recovers on its own.
    const socket = ui.state.admin;
    socket.attempt = 99;
    socket.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'closed', reason: 'gone' } }),
    );
    assert.equal(socket.stopped, false, 'the shell must not stop trying');
  });
});
