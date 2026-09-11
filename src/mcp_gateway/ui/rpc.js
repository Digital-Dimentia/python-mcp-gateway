// Two JSON-RPC-over-WebSocket clients, and the access key they both carry.
//
// `/admin` has no handshake: the method table is fixed and a method may be called the
// moment the socket opens. `/mcp` is MCP, so it must `initialize` and then send
// `notifications/initialized` before anything else is allowed — the gateway's session.py
// refuses every other method until it has.
//
// Both reconnect on their own. A gateway restart is the ordinary case (that is what
// `make run` and a SIGHUP look like from here), and a UI that needs a page reload
// afterwards is a UI that lies about the daemon being down.

const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000];

export const PROTOCOL_VERSION = '2025-06-18';

/** One JSON-RPC connection. Subclasses decide what "ready" means. */
export class RpcSocket extends EventTarget {
  constructor(path, key) {
    super();
    this.path = path;
    this.key = key || null;
    this.state = 'closed';
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.attempt = 0;
    this.stopped = false;
  }

  url() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = this.key ? `?key=${encodeURIComponent(this.key)}` : '';
    return `${scheme}//${location.host}${this.path}${query}`;
  }

  connect() {
    this.stopped = false;
    this.setState('connecting');
    let ws;
    try {
      ws = new WebSocket(this.url());
    } catch (err) {
      this.fail(String(err));
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.onOpen();
    };
    ws.onmessage = (event) => this.receive(event.data);
    ws.onerror = () => {
      // A browser deliberately tells a page nothing about *why* a socket failed — a 401
      // and a refused connection are indistinguishable here. So the UI cannot claim the
      // key was wrong; it can only say the socket did not open. `app.js` offers the key
      // field on a failure that happens before any successful open, which is the closest
      // honest approximation.
    };
    ws.onclose = (event) => {
      const wasOpen = this.state === 'open' || this.state === 'ready';
      for (const [, entry] of this.pending) {
        entry.reject(new RpcError(-32000, 'connection closed', null));
      }
      this.pending.clear();
      this.fail(event.reason || (wasOpen ? 'connection closed' : 'could not connect'));
    };
  }

  /** Stop reconnecting and close. */
  close() {
    this.stopped = true;
    if (this.ws) this.ws.close();
  }

  onOpen() {
    this.setState('open');
    this.setState('ready');
  }

  fail(reason) {
    this.ws = null;
    this.setState('closed', reason);
    if (this.stopped) return;
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    setTimeout(() => { if (!this.stopped) this.connect(); }, delay);
  }

  setState(state, reason) {
    this.state = state;
    this.dispatchEvent(new CustomEvent('state', { detail: { state, reason } }));
  }

  receive(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (message.id !== undefined && message.id !== null && !message.method) {
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(RpcError.from(message.error));
      else entry.resolve(message.result ?? {});
      return;
    }
    if (message.method) {
      this.dispatchEvent(new CustomEvent('notify', {
        detail: { method: message.method, params: message.params || {} },
      }));
      // A server->client *request* (sampling, roots, elicitation) is answered with
      // method-not-found rather than ignored: an unanswered request leaves the gateway
      // holding a future until its timeout. The UI declares no such capabilities, so a
      // well-behaved gateway never sends one; this is for the case where one does.
      if (message.id !== undefined && message.id !== null) {
        this.send({
          jsonrpc: '2.0',
          id: message.id,
          error: { code: -32601, message: `${message.method} is not supported by this client` },
        });
      }
    }
  }

  send(message) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  notify(method, params) {
    this.send({ jsonrpc: '2.0', method, params: params ?? {} });
  }

  /** Send a request. Resolves with the result, rejects with an RpcError. */
  request(method, params, { timeoutMs = 120000 } = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      if (!this.send({ jsonrpc: '2.0', id, method, params: params ?? {} })) {
        reject(new RpcError(-32000, 'not connected', null));
        return;
      }
      const timer = setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        // Tell the far side to stop working on it. MCP's own cancellation, and the
        // gateway propagates it all the way down to the backend's wire.
        this.notify('notifications/cancelled', { requestId: id, reason: 'client timeout' });
        reject(new RpcError(-32000, `no answer in ${Math.round(timeoutMs / 1000)}s`, null));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (err) => { clearTimeout(timer); reject(err); },
      });
    });
  }
}

/** `/admin`: no handshake, by design. See admin_channel.py. */
export class AdminSocket extends RpcSocket {
  constructor(key) { super('/admin', key); }
}

/** `/mcp`: a real MCP client, so what the UI shows is what the model would see. */
export class McpSocket extends RpcSocket {
  constructor(key) {
    super('/mcp', key);
    this.serverInfo = null;
    this.capabilities = {};
  }

  async onOpen() {
    this.setState('open');
    try {
      const result = await this.request('initialize', {
        protocolVersion: PROTOCOL_VERSION,
        // Declared honestly and minimally. The gateway takes the union of its clients'
        // capabilities and declares *that* downward, so claiming `sampling` here would
        // make a backend ask this page to run a model.
        capabilities: {},
        clientInfo: { name: 'mcp-gateway-ui', title: 'MCP Gateway UI', version: '0.1.0' },
      });
      this.serverInfo = result.serverInfo || null;
      this.capabilities = result.capabilities || {};
      this.notify('notifications/initialized', {});
      this.setState('ready');
    } catch (err) {
      this.setState('closed', err.message);
      if (this.ws) this.ws.close();
    }
  }
}

export class RpcError extends Error {
  constructor(code, message, data) {
    super(message);
    this.name = 'RpcError';
    this.code = code;
    this.data = data ?? null;
  }

  static from(error) {
    return new RpcError(error.code ?? -32000, error.message || 'error', error.data ?? null);
  }
}
