// The transport for the desktop shell, and nothing at all in a browser.
//
// The page cannot open the gateway's sockets itself. It has no access key -- the shell
// mints one per launch and keeps it in Rust -- and even if it had one, a `WebSocket`
// constructor takes a URL and a subprotocol list, with no way to send the
// `Authorization: Bearer` header the gateway wants. So the host opens both sockets and this
// module is the wire to them. See `desktop/README.md` and python-mcp-gateway-isb.
//
// Loaded unconditionally by `index.html`, because there is **one copy of the admin UI** and
// two hosts that serve it. In a browser the guard below finds no Tauri and the module
// returns, having done nothing; `webui.py` serves this file to a browser that will never
// use it, which costs one small GET and keeps the alternative -- a second, drifting copy of
// the assets -- off the table.

import { setTransport } from './rpc.js';

/** Whether this page is running inside the desktop shell. */
export const inShell = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

/**
 * One socket, as `rpc.js` expects it: `send`, `close`, a numeric `readyState`, and the four
 * handlers it assigns. The narrow half of the `WebSocket` interface, which is all that file
 * ever touches.
 *
 * The id is minted here rather than returned by `gw_open`, and that is load-bearing. `invoke`
 * is a promise, and the host pushes its `open` frame down the channel as soon as the
 * handshake completes -- which can be before that promise settles. `McpSocket` sends
 * `initialize` the instant it sees `open`, so a `send` that had to wait for an id would drop
 * the first frame of the MCP handshake. Handing the id *down* means there is nothing to wait
 * for.
 */
class ShellSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;

  constructor(path) {
    const { invoke, Channel } = window.__TAURI__.core;
    this.path = path;
    this.readyState = ShellSocket.CONNECTING;
    this.id = crypto.randomUUID();
    this._invoke = invoke;

    const channel = new Channel();
    channel.onmessage = (frame) => {
      if (frame.kind === 'open') {
        this.readyState = ShellSocket.OPEN;
        if (this.onopen) this.onopen();
      } else if (frame.kind === 'frame') {
        if (this.onmessage) this.onmessage({ data: frame.text });
      } else if (frame.kind === 'close') {
        this._die(frame.reason);
      }
    };

    // No key argument anywhere in this call. The host holds it; the window has never seen
    // one and has nowhere to put one.
    invoke('gw_open', { id: this.id, path, onFrame: channel })
      .catch((err) => this._die(String(err)));
  }

  /** Exactly one close, whoever noticed first. */
  _die(reason) {
    if (this.readyState === ShellSocket.CLOSED) return;
    this.readyState = ShellSocket.CLOSED;
    if (this.onclose) this.onclose({ reason: reason || 'connection closed' });
  }

  send(text) {
    // Optimistic, because `invoke` is async and there is no readyState on the far side to
    // consult. That is honest rather than sloppy: a browser writing into a socket that
    // closes a millisecond later behaves identically, and `rpc.js` covers it from both ends
    // -- every pending request is rejected on close, and each one has its own timeout.
    this._invoke('gw_send', { id: this.id, text }).catch((err) => this._die(String(err)));
  }

  close() {
    this._invoke('gw_close', { id: this.id }).catch(() => { /* already gone */ });
    this._die('closed by the window');
  }
}

/** What the gateway is doing, for the status line. Null in a browser. */
export async function gatewayStatus() {
  if (!inShell) return null;
  try {
    return await window.__TAURI__.core.invoke('gw_status');
  } catch {
    return null;
  }
}

/** Call `handler` whenever the supervisor changes state. Returns an unsubscribe function. */
export async function onGatewayState(handler) {
  if (!inShell || !window.__TAURI__.event) return () => {};
  return window.__TAURI__.event.listen('gateway-state', (event) => handler(event.payload));
}

/**
 * Put the deployment's own title on the desktop window. A no-op in a browser, where
 * `document.title` is the whole story.
 *
 * The window's title is baked into `tauri.conf.json` at build time and is therefore the
 * stock one in a bundle anybody can build; the branding block is read by the *daemon*, at
 * run time, out of a config file the bundle never saw. So the page is the only thing that
 * knows both, and this is the one call that closes that gap. `core:window:allow-set-title`
 * is in the capability file for exactly this and nothing else -- see branding.md.
 *
 * Swallows its failure: a window whose title bar still says `MCP Gateway` is a cosmetic
 * disappointment, and throwing here would take `refreshAdmin` down with it.
 */
export async function setShellTitle(title) {
  if (!inShell || !window.__TAURI__?.window || !title) return;
  try {
    await window.__TAURI__.window.getCurrentWindow().setTitle(title);
  } catch {
    /* the capability is absent, or the window has gone */
  }
}

if (inShell) {
  // The key argument `rpc.js` passes is ignored, and that is the point of the whole design.
  setTransport((path) => new ShellSocket(path));
}
