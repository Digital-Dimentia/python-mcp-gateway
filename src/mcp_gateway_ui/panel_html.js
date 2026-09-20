// SEP-1865's HTML tier: a backend's own document, framed, and the bridge it talks over.
//
// The declarative tier next door (`panel_declarative.js`) turns JSON into DOM and executes
// nothing. This one runs a document a backend wrote, so every line here is either the wall
// around it or the one door through it.
//
// ## The wall is not in this file
//
// It is the response header on `/panel/<token>` -- `Content-Security-Policy: sandbox
// allow-scripts` -- which puts the document in an opaque origin however it is reached. The
// `sandbox` attribute below is set anyway, because two mechanisms are better than one, but
// it is the weaker of the two: an attribute belongs to whoever wrote the embed, and a header
// belongs to the document. See `src/mcp_gateway/panels.md`.
//
// What this file must get right is the consequence: **an opaque origin serialises as the
// string `"null"`.**
//
//   * Identity is never `event.origin`. It is the `MessagePort` this module created and
//     handed to exactly one window, and, for the one message that arrives before the port
//     exists, `event.source === iframe.contentWindow`. An origin check here would either
//     compare against `"null"` -- which every sandboxed frame on the page shares, so it
//     proves nothing -- or fail closed and break the handshake.
//   * The port handout uses `targetOrigin: '*'`, and has to: `"null"` is not a legal
//     `targetOrigin`, and there is no spelling of "this opaque origin". The `'*'` is safe
//     because the *recipient* is named -- `iframe.contentWindow`, not a broadcast -- and
//     because the only thing sent this way is the port itself.
//
// ## The door
//
// One `MessagePort`, carrying MCP-shaped JSON-RPC. A panel may ask for `tools/call`, and
// that request goes through the same three gates a declarative panel's `action` does --
// scope, consent, budget -- because it is the same `onAction` from `screens/basics/panel.js`.
// A panel cannot make a call you do not see: the answer lands on its own result card.
//
// Everything else across the port is presentation: the handshake, the tool input and result
// the panel is about, the height it would like to be, and the theme it is being shown in.
// None of that reaches the gateway.

import { el, renderToolResult } from './render.js';
import { inShell } from './tauri-transport.js';

/**
 * Whether this host can frame a panel at all.
 *
 * False in the desktop window, and that is a fact about the window rather than a setting:
 * its content policy is `connect-src ipc:` with no `frame-src`, the page is loaded from
 * `tauri://localhost`, and the daemon's origin is not one it can reach. A panel URL is
 * relative -- in the shell it would resolve against `tauri://localhost` and find nothing.
 *
 * So the shell says so. Making it work means a `register_uri_scheme` handler in Rust
 * serving a staged document under the same sandbox header, which is python-mcp-gateway-u5s.5;
 * until then, "this window cannot show this" beats an empty box. The declarative tier is
 * unaffected in both hosts, which is most of why it exists.
 */
export const canFramePanels = !inShell;

/** The `profile` parameter this module renders. SEP-1865's own spelling. */
export const PROFILE = 'mcp-app';

/** What the frame starts at, and the range a panel may ask to be resized within. */
export const DEFAULT_HEIGHT = 320;
export const MIN_HEIGHT = 80;
export const MAX_HEIGHT = 1600;

/** What a panel is told it is talking to. */
export const HOST_INFO = { name: 'mcp-gateway-ui', title: 'MCP Gateway UI', version: '0.1.0' };

/** SEP-1865 rides on MCP's own version. The same string `rpc.js` sends at `initialize`. */
export const UI_PROTOCOL_VERSION = '2025-06-18';

/** Whether a content item's `mimeType` names this tier. */
export function isHtmlPanel(mimeType) {
  if (typeof mimeType !== 'string') return false;
  const [type, ...parameters] = mimeType.split(';');
  if (type.trim().toLowerCase() !== 'text/html') return false;
  return parameters.some((part) => {
    const [key, value] = part.split('=');
    return key.trim().toLowerCase() === 'profile' && value?.trim().replace(/"/g, '') === PROFILE;
  });
}

/** The theme a panel is being shown in, so it can match without guessing. */
function hostContext() {
  return {
    theme: document.documentElement.getAttribute('data-theme') || 'system',
    hostInfo: HOST_INFO,
  };
}

/**
 * Speak to one panel over one port.
 *
 * Exported and taking a port rather than an iframe so it can be driven directly: a
 * `MessageChannel` in a test is the same object this hands to a frame, which means the
 * handshake can be exercised without a browser to load a document in. See
 * `tests/ui/panel_html.test.mjs`.
 *
 * Returns `{ close }`. Everything else it does, it does through the callbacks.
 */
export function bridge({ port, call, result, onAction, onResize, onNote }) {
  let initialized = false;

  const send = (message) => {
    try {
      port.postMessage(message);
    } catch {
      // A frame that has gone away is not an error worth showing: the card is still there
      // with the result on it, which is the thing the person asked for.
    }
  };
  const notify = (method, params) => send({ jsonrpc: '2.0', method, params });
  const answer = (id, payload) => send({ jsonrpc: '2.0', id, ...payload });

  /** The two notifications a panel exists to receive: what was called, and what came back. */
  const deliver = () => {
    notify('ui/notifications/tool-input', { name: call?.name, arguments: call?.arguments ?? {} });
    notify('ui/notifications/tool-result', { result });
  };

  async function handle(message) {
    const { id, method, params } = message || {};
    if (method === 'ui/initialize') {
      // Answered whether or not the panel has sent it before. Refusing a second handshake
      // would be a rule with nothing behind it: the panel is already framed, and the only
      // thing it could achieve is re-reading its own tool result.
      answer(id, {
        result: {
          protocolVersion: UI_PROTOCOL_VERSION,
          hostInfo: HOST_INFO,
          // What the panel may ask for, stated rather than discovered by trying: one
          // method, and it is gated. `resources`, `prompts` and `sampling` are absent
          // because a panel has no route to them at all.
          capabilities: { tools: { call: true } },
          hostContext: hostContext(),
        },
      });
      return;
    }
    if (method === 'ui/notifications/initialized') {
      if (initialized) return;
      initialized = true;
      deliver();
      return;
    }
    if (method === 'ui/notifications/size-changed') {
      const height = Number(params?.height);
      if (Number.isFinite(height)) {
        onResize?.(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.round(height))));
      }
      return;
    }
    if (method === 'tools/call') {
      if (id === undefined) return;             // a notification cannot ask for a call
      try {
        const answered = await onAction({ tool: params?.name, arguments: params?.arguments ?? {} });
        answer(id, { result: answered });
      } catch (error) {
        // The refusal reaches the panel as a JSON-RPC error and the page as a note, so a
        // panel that swallows the error still leaves the person told. -32000 is the
        // implementation-defined range: this is a host decision, not a malformed request.
        onNote?.(error.message);
        answer(id, { error: { code: -32000, message: error.message } });
      }
      return;
    }
    if (id !== undefined) {
      answer(id, { error: { code: -32601, message: `Method not found: ${method}` } });
    }
  }

  port.onmessage = (event) => {
    // No origin check, on purpose -- see the header. Arrival on this port *is* the identity:
    // it was handed to one window and nothing can forge a port it was not given.
    handle(event.data);
  };
  port.start?.();

  //: The theme is the one piece of host context that changes while a panel is open, and it
  //: changes by an attribute on `<html>`. Watching it is cheaper than a bus nothing else
  //: would use, and disconnects with the card.
  const watcher = new MutationObserver(() => {
    if (initialized) notify('ui/notifications/host-context-changed', hostContext());
  });
  watcher.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  return {
    close() {
      watcher.disconnect();
      port.onmessage = null;
      try { port.close(); } catch { /* already gone */ }
    },
  };
}

/**
 * Frame one HTML panel, and wire the bridge to it.
 *
 * Returns the node to put on a card straight away; the URL is minted and the document loads
 * behind it, because a card that appears only once a round trip has finished looks like a
 * click that did nothing.
 *
 * `mint` is the only route to `/admin` in the panel code, and it is handed in rather than
 * imported so that route is visible in `app.js` beside the others. It takes the panel's
 * `mcpgw://` URI and answers with `{ url }` -- a short-lived, single-use capability URL.
 */
export function renderHtmlPanel({ reference, call, result, onAction, mint }) {
  if (!canFramePanels) {
    return el('div', { class: 'panel-html' }, [
      el('p', {
        class: 'note',
        text: 'This panel is SEP-1865\u2019s HTML tier, which this window cannot frame. '
          + 'Open the same gateway in a browser to see it; showing the result instead.',
      }),
      renderToolResult(result),
    ]);
  }
  const note = el('p', { class: 'note' });
  const frame = el('iframe', {
    class: 'panel-frame',
    // Weaker than the response header and set anyway; see the module header. `allow-scripts`
    // alone: `allow-same-origin` here would hand back the origin the header took away.
    sandbox: 'allow-scripts',
    height: String(DEFAULT_HEIGHT),
    title: 'Panel',
    referrerpolicy: 'no-referrer',
  });
  const body = el('div', { class: 'panel-html' }, [frame, note]);

  let channel = null;
  let link = null;

  const say = (text) => { note.textContent = text; };

  //: Handed the moment the document is there to receive it. `'*'` is required: an opaque
  //: origin is `"null"`, which is not a legal `targetOrigin` -- see the module header.
  const handPort = () => {
    if (channel) return;
    channel = new MessageChannel();
    link = bridge({
      port: channel.port1,
      call,
      result,
      onAction,
      onResize: (height) => frame.setAttribute('height', String(height)),
      onNote: say,
    });
    frame.contentWindow?.postMessage({ type: 'ui/port' }, '*', [channel.port2]);
  };

  frame.addEventListener('load', handPort);
  // A panel that announces itself before we saw the load event still gets its port. The
  // source check is the identity -- never `event.origin`, which reads `"null"` here.
  window.addEventListener('message', (event) => {
    if (event.source && event.source === frame.contentWindow) handPort();
  });

  //: Handed back so the card can drop the bridge when it is removed. A closed port is what
  //: stops a panel whose card is gone from still answering.
  body.closePanel = () => { link?.close(); };

  mint(reference).then(
    (minted) => { frame.setAttribute('src', minted.url); },
    (error) => { say(`That panel could not be opened: ${error.message}`); },
  );

  return body;
}
