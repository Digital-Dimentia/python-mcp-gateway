// The overlay that stands in front of the page until the sockets are up.
//
// It has one job and it used to get it wrong in the most visible place there is: the first
// second of every launch. A daemon that is still binding refuses connections, which at this
// level is indistinguishable from a wrong access key -- and the page said "this gateway may
// require an access key", then connected a moment later and cleared it. Every start looked
// like a failure that fixed itself.
//
// So a closed socket is only evidence that we are *still connecting* until the retry budget
// is spent. This file is the browser half of that; `shell.test.mjs` is the desktop half,
// where the host's own state events outrank anything a socket can say.

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

describe('the gate', () => {
  let ui;
  let window;
  let document;

  before(async () => {
    ({ ui, window, document } = await boot());
  });

  const close = (socket) => socket.dispatchEvent(
    new window.CustomEvent('state', { detail: { state: 'closed', reason: 'refused' } }),
  );

  it('starts as a splash, before any socket has had a chance to answer', () => {
    // Visible from the first paint rather than hidden. The alternative is a blank page that
    // suddenly becomes an error, which is the same startle in a different costume.
    const gate = document.getElementById('gate');
    assert.equal(gate.dataset.mode, 'connecting');
    assert.doesNotMatch(document.getElementById('gate-message').textContent, /key/i);
  });

  it('stays a splash for the first closes', () => {
    const socket = ui.state.admin;
    socket.attempt = 1;
    close(socket);
    const gate = document.getElementById('gate');
    assert.equal(gate.dataset.mode, 'connecting');
    assert.equal(document.getElementById('gate-key-field').hidden, true);
    assert.doesNotMatch(document.getElementById('gate-message').textContent, /key/i);
  });

  it('asks for a key only once the retries are spent', () => {
    const socket = ui.state.admin;
    socket.attempt = 3;
    close(socket);
    const gate = document.getElementById('gate');
    assert.equal(gate.dataset.mode, 'blocked');
    assert.equal(document.getElementById('gate-key-field').hidden, false);
    assert.match(document.getElementById('gate-message').textContent, /access key/i);
  });

  it('does not fall back to a splash once it has asked', () => {
    // A socket that closes again after the budget is spent knows nothing new. Letting it
    // repaint the splash would hide the field the user is meant to type into.
    const socket = ui.state.admin;
    socket.attempt = 1;
    close(socket);
    assert.equal(document.getElementById('gate').dataset.mode, 'blocked');
  });

  it('goes away when a socket comes up', () => {
    ui.state.admin.dispatchEvent(
      new window.CustomEvent('state', { detail: { state: 'ready' } }),
    );
    assert.equal(document.getElementById('gate').hidden, true);
  });

  it('shows a splash again on a retry rather than the stale panel', () => {
    document.getElementById('gate-form').dispatchEvent(
      new window.Event('submit', { cancelable: true, bubbles: true }),
    );
    const gate = document.getElementById('gate');
    assert.equal(gate.hidden, false);
    assert.equal(gate.dataset.mode, 'connecting');
  });
});
