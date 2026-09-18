// The Connection screen: which gateway this window is driving.
//
// Two answers. **Local** is the child the desktop host starts — the app as it has always
// been, and what you get when nothing is configured. **Remote** is a daemon somebody else
// is running on another machine, reached through an SSH port forward the host opens and
// supervises. The backends and every credential stay over there; what comes across the
// tunnel is the same two WebSockets the local child answers on.
//
// ## Why this screen exists only in the desktop app
//
// The forward is a child process, and a browser tab has none. So the `<option>` in the
// header carries `data-shell-only` and `app.js` removes it when there is no host behind the
// page — see its screen registry. This module is still served to a browser, because there is
// one copy of the admin UI and two hosts for it (the same rule `tauri-transport.js` follows);
// it simply never gets shown there.
//
// ## Why there is no key field, and no password field
//
// In remote mode the daemon over there binds `127.0.0.1` with **no access key**, and SSH is
// the authentication: if you can open a forward to that machine you are already someone that
// machine trusts. So there is no credential to type here and none stored on this Mac —
// which is the same promise the local mode makes, arrived at from the other direction.
//
// Everything about *how* to reach the machine beyond its name belongs in `~/.ssh/config`:
// the login, a non-standard port, an identity file, a jump host. A second place to configure
// SSH is a second place to get SSH wrong.

import {
  connectionApply,
  connectionGet,
  connectionSave,
  inShell,
} from '../../tauri-transport.js';

//: The same 12-line shim every module in this UI that builds DOM carries; see
//: `screens/about/tour.js` for why they copy it rather than share it.
const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
};

//: What the form is editing. Read from the host on the first visit and kept here, so typing
//: survives a repaint driven by a `gateway-state` event -- which, while a tunnel is coming
//: up, arrives every few hundred milliseconds.
let draft = null;
//: What the host last confirmed it had stored. The Save button is disabled while these two
//: agree, which is the only honest way to say "there is nothing to apply".
let stored = null;
let notice = null;

/** The default a field falls back to when the host has told us nothing yet. */
const BLANK = { mode: 'local', destination: '', localPort: 8765, remotePort: 8765 };

/** One labelled control, with the sentence that says what it is for underneath. */
function field(label, control, help) {
  return el('label', { class: 'conn-field' }, [
    el('span', { class: 'conn-label', text: label }),
    control,
    help ? el('span', { class: 'conn-help', text: help }) : null,
  ]);
}

function text(name, value, placeholder) {
  const input = el('input', {
    type: 'text', value: value || '', placeholder: placeholder || '', spellcheck: false,
  });
  input.addEventListener('input', () => {
    draft[name] = input.value.trim();
    render();
  });
  return input;
}

function port(name, value) {
  const input = el('input', { type: 'number', min: '1', max: '65535', value: String(value) });
  input.addEventListener('input', () => {
    draft[name] = Number(input.value) || 0;
    render();
  });
  return input;
}

/** Local or remote, and the one sentence each that says what you are choosing. */
function modeCard() {
  const choice = (mode, title, blurb) => {
    const radio = el('input', { type: 'radio', name: 'conn-mode', checked: draft.mode === mode });
    radio.addEventListener('change', () => {
      draft.mode = mode;
      //: Switching to remote with the ports still at zero would be a form that refuses to
      //: save and does not say why. A forward needs both ends, so give it both.
      if (mode === 'remote') {
        draft.localPort = draft.localPort || 8765;
        draft.remotePort = draft.remotePort || 8765;
      }
      render();
    });
    return el('label', { class: `conn-choice${draft.mode === mode ? ' on' : ''}` }, [
      radio,
      el('span', {}, [
        el('strong', { text: title }),
        el('span', { class: 'conn-help', text: blurb }),
      ]),
    ]);
  };

  return el('section', { class: 'conn-card' }, [
    el('h2', { text: 'Where the gateway runs' }),
    choice(
      'local',
      'On this machine',
      'This app starts the gateway and stops it with the window. Backends and credentials '
      + 'are here, and nothing leaves the machine.',
    ),
    choice(
      'remote',
      'On another machine, over SSH',
      'You run the daemon there — systemd, or `make run`. This app opens an SSH tunnel to '
      + 'it and never starts, stops or restarts it.',
    ),
  ]);
}

/** Where that other machine is. Only shown when it is one. */
function remoteCard() {
  if (draft.mode !== 'remote') return null;
  return el('section', { class: 'conn-card' }, [
    el('h2', { text: 'The remote gateway' }),
    field(
      'SSH destination',
      text('destination', draft.destination, 'build-box'),
      'Exactly what you would type after `ssh`. Anything else — a login, a port, an '
      + 'identity file, a jump host — goes in ~/.ssh/config under a Host block, and its '
      + 'alias goes here.',
    ),
    field(
      'Port over there',
      port('remotePort', draft.remotePort),
      'What the daemon binds on that machine. It should bind 127.0.0.1 and need no access '
      + 'key: the tunnel is what gets you in.',
    ),
    field(
      'Port on this Mac',
      port('localPort', draft.localPort),
      'The near end of the forward, and where your own clients attach. Keep it the same '
      + 'number as the far end unless something here already has it.',
    ),
  ]);
}

/** What the host is doing right now, and — when it went wrong — what to do about it. */
function stateCard(state) {
  //: Two halves of one payload: `shell` is what the host is *doing* (the phase, and the
  //: sentence that goes with it) and `connection` is *where* -- see `state.shell` in
  //: `app.js`. Both are absent until the host's first answer, which is the state this
  //: screen opens in.
  const shell = state.shell || {};
  const status = state.connection || {};
  const phase = shell.phase || 'idle';
  const dot = {
    idle: 'stopped',
    'starting-daemon': 'starting',
    'opening-tunnel': 'starting',
    'tunnel-up': 'starting',
    'far-side-silent': 'failed',
    listening: 'running',
    restarting: 'starting',
    failed: 'failed',
  }[phase] || 'stopped';

  const rows = [
    el('div', { class: 'conn-state' }, [
      el('span', { class: `dot dot-${dot}` }),
      el('span', { text: shell.detail || (phase === 'listening' ? 'Connected.' : 'Not connected.') }),
    ]),
  ];

  //: The line to paste into a client, and the reason the local port is a number somebody
  //: chose rather than one the OS handed out. Shown in local mode too when a port is pinned
  //: -- nothing else in this app has ever told anyone how to attach a client.
  if (status.connectCommand) {
    rows.push(el('div', { class: 'conn-paste' }, [
      el('p', { class: 'conn-help', text: 'Attach a client on this machine:' }),
      el('pre', { class: 'conn-code', text: status.connectCommand }),
    ]));
  } else if (draft.mode === 'local' && !draft.localPort) {
    rows.push(el('p', {
      class: 'conn-help',
      text: 'The gateway takes whatever port is free, so there is no address to give a '
        + 'client. Set a port on this Mac above if you want to attach one.',
    }));
  }

  return el('section', { class: 'conn-card' }, [el('h2', { text: 'Now' }), ...rows]);
}

/** Save, and apply. Two buttons, because they are two decisions. */
function actions() {
  const changed = JSON.stringify(draft) !== JSON.stringify(stored);

  const save = el('button', {
    type: 'button', class: 'primary', text: 'Save', disabled: !changed,
  });
  save.addEventListener('click', async () => {
    try {
      stored = await connectionSave(draft);
      draft = { ...stored };
      notice = { kind: 'ok', text: 'Saved. Connect to start using it.' };
    } catch (err) {
      //: The host's own words. Every refusal from `settings::validate` is a sentence
      //: written to be read by whoever typed the thing it is refusing.
      notice = { kind: 'bad', text: String(err) };
    }
    render();
  });

  const apply = el('button', {
    type: 'button', class: 'ghost', text: 'Connect',
    title: 'Tear down what is running and start again from the saved settings',
  });
  apply.addEventListener('click', async () => {
    notice = { kind: 'ok', text: 'Reconnecting…' };
    render();
    try {
      await connectionApply();
    } catch (err) {
      notice = { kind: 'bad', text: String(err) };
      render();
    }
  });

  return el('div', { class: 'conn-actions' }, [
    save,
    apply,
    notice ? el('span', { class: `conn-notice ${notice.kind}`, text: notice.text }) : null,
  ]);
}

/** The whole screen, rebuilt. */
let latest = {};

function render() {
  const root = document.getElementById('connection');
  if (!root) return;

  if (!inShell) {
    //: A browser has no host to run `ssh`, so there is nothing here to configure. The
    //: sentence is what someone sees for the one frame before `app.js` corrects a stored
    //: screen name -- see `index.html`.
    root.replaceChildren(el('p', {
      class: 'conn-note',
      text: 'The Connection screen is part of the desktop app.',
    }));
    return;
  }

  draft = draft || { ...BLANK };
  //: Which field has focus, and where the caret is. The host emits `gateway-state` every
  //: few hundred milliseconds while a tunnel comes up, and a screen that rebuilt itself
  //: under a typing hand would be unusable at exactly the moment it is being used.
  const active = document.activeElement;
  const marker = active && root.contains(active)
    ? { label: active.closest('.conn-field')?.querySelector('.conn-label')?.textContent,
        start: active.selectionStart, end: active.selectionEnd }
    : null;

  root.replaceChildren(
    el('div', { class: 'conn-inner-body' }, [
      modeCard(),
      remoteCard(),
      stateCard(latest),
      actions(),
    ]),
  );

  if (marker?.label) {
    for (const node of root.querySelectorAll('.conn-field')) {
      if (node.querySelector('.conn-label')?.textContent !== marker.label) continue;
      const input = node.querySelector('input');
      if (!input) break;
      input.focus();
      try {
        input.setSelectionRange(marker.start, marker.end);
      } catch {
        //: A number input refuses a selection range in some engines. Focus is the half that
        //: matters; the caret is a nicety.
      }
      break;
    }
  }
}

export default {
  id: 'connection',

  //: Handed the same state every screen is. What it renders from is mostly the *shell's*
  //: status rather than the daemon's -- `state.connection` is what `app.js` stashed off the
  //: last `gateway-state` -- because a remote daemon reports `127.0.0.1:8765` exactly as a
  //: local one does, and cannot know it is being tunnelled.
  refresh(state) {
    latest = state;
    render();
  },

  //: The settings are asked for once, when the screen is first looked at, rather than at
  //: load: a window that opens on Basics should not be invoking host commands about a
  //: screen nobody has asked to see.
  async show() {
    if (!inShell || stored) {
      render();
      return;
    }
    const settings = await connectionGet();
    if (settings) {
      stored = settings;
      draft = { ...settings };
    }
    render();
  },
};
