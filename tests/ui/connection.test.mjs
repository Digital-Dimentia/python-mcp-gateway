// The Connection screen, and what the window does when the tunnel will not open.
//
// Its own file because `boot()` is once per process -- see `harness.mjs` -- and booted as
// the desktop shell, because this screen exists nowhere else. What is asserted is the
// wiring: which settings the page asks the host to store, what it never stores, which
// sentence each state produces, and -- the one that matters most -- that a failed remote
// connection still leaves a way back to this screen.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

let ui;
let document;
let host;
let window;

const screen = () => document.getElementById('connection');
const gate = () => document.getElementById('gate');

/** The host's last word, as the page would have received it. */
function announce(connection, rest = {}) {
  host.emit('gateway-state', {
    state: 'starting',
    phase: 'opening-tunnel',
    attempt: 0,
    reason: null,
    detail: null,
    log: [],
    ...rest,
    connection,
  });
}

const LOCAL = { mode: 'local', label: null, localPort: 0, remotePort: 8765, connectCommand: null };
const REMOTE = {
  mode: 'remote',
  label: 'dave@build-box',
  localPort: 8765,
  remotePort: 8765,
  connectCommand:
    'claude mcp add gateway -- /opt/MCP-Gateway.app/Contents/Resources/python/bin/'
    + 'mcp-gateway-connect --url ws://127.0.0.1:8765/mcp',
};

before(async () => {
  ({ ui, document, host, window } = await boot({ shell: true }));
});

after(() => {
  ui.state.admin.close();
  ui.state.mcp.close();
});

describe('the screen', () => {
  it('is offered in the shell, unlike in a browser', () => {
    assert.ok(ui.AVAILABLE.includes('connection'));
    assert.ok(document.querySelector('#screen-select option[value="connection"]'));
  });

  it('asks the host for the stored settings the first time it is shown, and not before', async () => {
    assert.equal(host.calls.filter((c) => c.command === 'conn_settings').length, 0);
    ui.showScreen('connection');
    await new Promise((resume) => setTimeout(resume, 0));
    assert.equal(host.calls.filter((c) => c.command === 'conn_settings').length, 1);
  });

  it('opens on local, and says what local means', () => {
    const text = screen().textContent;
    assert.match(text, /On this machine/);
    assert.match(text, /On another machine, over SSH/);
    // Local mode has nothing to configure, so the remote fields are not there at all.
    assert.equal(screen().querySelector('input[type="text"]'), null);
  });
});

describe('choosing a remote gateway', () => {
  const chooseRemote = () => {
    const remote = [...screen().querySelectorAll('.conn-choice')]
      .find((node) => node.textContent.includes('over SSH'));
    remote.querySelector('input').checked = true;
    remote.querySelector('input').dispatchEvent(new window.Event('change', { bubbles: true }));
  };

  const type = (label, value) => {
    const field = [...screen().querySelectorAll('.conn-field')]
      .find((node) => node.querySelector('.conn-label').textContent === label);
    const input = field.querySelector('input');
    input.value = value;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  };

  it('reveals the destination and both ports, already defaulted to the usual one', () => {
    chooseRemote();
    const labels = [...screen().querySelectorAll('.conn-label')].map((n) => n.textContent);
    assert.deepEqual(labels, ['SSH destination', 'Port over there', 'Port on this Mac']);
    // A forward needs both ends, and a form that refuses to save without saying why is a
    // worse answer than a sensible default.
    const values = [...screen().querySelectorAll('input[type="number"]')].map((n) => n.value);
    assert.deepEqual(values, ['8765', '8765']);
  });

  it('never offers anywhere to type a credential', () => {
    // Remote mode's whole authentication story is SSH: the daemon over there binds loopback
    // with no key, and this app stores nothing. A password field here would be the feature
    // going wrong in the one way that matters.
    assert.equal(screen().querySelector('input[type="password"]'), null);
    for (const label of [...screen().querySelectorAll('.conn-label')]) {
      assert.doesNotMatch(label.textContent, /key|password|passphrase|secret|token/i);
    }
  });

  it('saves exactly what was typed, and nothing else', async () => {
    type('SSH destination', 'dave@build-box');
    type('Port on this Mac', '9000');

    const save = [...screen().querySelectorAll('button')].find((b) => b.textContent === 'Save');
    assert.equal(save.disabled, false, 'there is something to save');
    save.click();
    await new Promise((resume) => setTimeout(resume, 0));

    const sent = host.calls.filter((c) => c.command === 'conn_save').pop();
    assert.equal(sent.args.settings.mode, 'remote');
    assert.equal(sent.args.settings.destination, 'dave@build-box');
    assert.equal(sent.args.settings.localPort, 9000);
    assert.equal(sent.args.settings.remotePort, 8765);
    assert.ok(
      !JSON.stringify(sent.args).match(/key|pass|secret|token/i),
      `no credential may reach the host: ${JSON.stringify(sent.args)}`,
    );
  });

  it('saving and connecting are two decisions', async () => {
    // A typo saved is a typo. A typo applied is a window with no gateway behind it, so the
    // page never applies one on the user's behalf.
    assert.equal(host.calls.filter((c) => c.command === 'conn_apply').length, 0);
    const connect = [...screen().querySelectorAll('button')].find((b) => b.textContent === 'Connect');
    connect.click();
    await new Promise((resume) => setTimeout(resume, 0));
    assert.equal(host.calls.filter((c) => c.command === 'conn_apply').length, 1);
  });

  it('shows the host\'s own words when it refuses what was typed', async () => {
    host.refuseNextSave('"-oProxyCommand=id" starts with a dash, which ssh would read as an option');
    type('SSH destination', '-oProxyCommand=id');
    const save = [...screen().querySelectorAll('button')].find((b) => b.textContent === 'Save');
    save.click();
    await new Promise((resume) => setTimeout(resume, 0));
    assert.match(screen().textContent, /starts with a dash/);
  });
});

describe('what it says is happening', () => {
  it('names the machine and hands over the line to paste', () => {
    announce(REMOTE, { state: 'listening', phase: 'listening', detail: null });
    ui.SCREEN_MODULES.connection.refresh(ui.state);
    const text = screen().textContent;
    assert.match(text, /Connected\./);
    // Built by the host from the bundle's own path -- a line assembled in JavaScript would
    // only ever work for whoever put the app in /Applications.
    assert.match(text, /ws:\/\/127\.0\.0\.1:8765\/mcp/);
    assert.match(text, /\/opt\/MCP-Gateway\.app/);
  });

  it('says plainly when the tunnel is fine and the far side is not', () => {
    // The common case, and the one that reads as "this app is broken" unless it is named.
    announce(REMOTE, {
      state: 'starting',
      phase: 'far-side-silent',
      detail: 'The tunnel is open, but nothing is listening on 127.0.0.1:8765 on '
        + 'dave@build-box. Start the gateway there.',
    });
    ui.SCREEN_MODULES.connection.refresh(ui.state);
    assert.match(screen().textContent, /nothing is listening/);
    assert.ok(screen().querySelector('.dot-failed'), 'and it reads as needing attention');
  });
});

describe('the way back from a failed tunnel', () => {
  it('offers a way to the settings, because the gate covers the header', () => {
    // Without this the app is unrecoverable from inside itself: the gate is fixed over the
    // whole window, the screen selector is underneath it, and a mistyped destination can
    // then only be fixed by hand-editing a file nobody has been told about.
    announce(REMOTE, {
      state: 'failed',
      phase: 'failed',
      reason: 'dave@build-box: Permission denied (publickey).',
      detail: 'dave@build-box: Permission denied (publickey). SSH is the only authentication here.',
    });

    assert.equal(gate().hidden, false);
    assert.equal(gate().dataset.mode, 'failed');
    assert.match(document.getElementById('gate-message').textContent, /Permission denied/);
    // Not "The gateway could not start": nothing was ever asked to start.
    assert.match(document.getElementById('gate-title').textContent, /remote gateway/i);
    // And still never a key field, and never the word.
    assert.equal(document.getElementById('gate-key-field').hidden, true);
    assert.doesNotMatch(document.getElementById('gate-message').textContent, /access key/i);

    const out = document.getElementById('gate-settings');
    assert.equal(out.hidden, false);
    out.click();
    assert.equal(gate().hidden, true);
    assert.equal(document.documentElement.getAttribute('data-screen'), 'connection');
  });

  it('and the next failing status does not slam it shut again', () => {
    // The host emits every few hundred milliseconds while it retries. A gate that came
    // straight back would undo the click that just asked to see this screen.
    announce(REMOTE, {
      state: 'failed',
      phase: 'failed',
      reason: 'dave@build-box: Permission denied (publickey).',
      detail: 'dave@build-box: Permission denied (publickey).',
    });
    assert.equal(gate().hidden, true);
  });

  it('is not offered when the gateway is the local child', () => {
    announce(LOCAL, { state: 'failed', phase: 'failed', reason: 'servers.yaml is not valid' });
    assert.equal(document.getElementById('gate-settings').hidden, true);
    assert.match(document.getElementById('gate-title').textContent, /could not start/i);
  });
});
