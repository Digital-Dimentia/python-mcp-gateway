// What the tour says. Content, in one file, on purpose.
//
// `tour.js` is the deck and knows nothing about this project; this is the part that is
// about *this* project, and it is the part that goes stale. Keeping it in one list means
// the edit after a feature lands is a paragraph in a slide rather than a hunt through a
// renderer -- and it means the question "is the tour still true?" has one file to read.
//
// ## The rule for what belongs here
//
// **Nothing here may be the only place a fact is written.** Every claim on these slides is
// the short form of something in `README.md`, `GET_STARTED.md`, `ARCHITECTURE.md` or a
// module's own `.md`, and the slide's job is to be the version you can read in the window
// you are already in. When the two disagree, the Markdown is right and the slide is a bug.
//
// ## And what does not
//
// No screenshots, and no images at all. The page ships what it serves -- see the asset
// allowlist in `webui.py` and the CSP -- so a tour built out of PNGs would be a tour that
// doubles the wire weight of the admin UI and rots the first time a column moves. The
// diagrams here are boxes and arrows built out of the DOM, which cost nothing, theme
// themselves, and can be read by a screen reader.
//
// A slide is `{ id, title, tag, render(state) }`. `render` is handed the same state object
// the screen is -- so a slide may quote the running gateway, and three of them do.

import { el } from './tour.js';

// ── Small renderers ────────────────────────────────────────────────────────────
//
// Five shapes, reused across nine slides. A slide that needs a sixth is a slide worth
// looking at twice: the value of a deck is that every card is read the same way.

/** A paragraph of plain prose. The default body of a slide. */
const p = (text) => el('p', { class: 'tour-p', text });

/** A row of titled cards — the shape for "here are four things, of equal weight". */
const cards = (items) => el('div', { class: 'tour-cards' }, items.map(([title, body]) => (
  el('div', { class: 'tour-card' }, [
    el('h4', { text: title }),
    el('p', { text: body }),
  ])
)));

/** Label/value pairs, for a list of names with one line each. */
const pairs = (items) => el('dl', { class: 'tour-pairs' }, items.flatMap(([term, def]) => [
  el('dt', { text: term }),
  el('dd', { text: def }),
]));

/** A literal block: a file, a command, a fragment of YAML. Never parsed, only shown. */
const code = (text) => el('pre', { class: 'tour-code' }, [el('code', { text })]);

/**
 * Boxes and arrows, in one direction.
 *
 * `role="list"` and one `listitem` per box: without it a screen reader reads a diagram as a
 * run-on sentence with arrows in it. The arrows themselves are `aria-hidden`, because
 * "right-pointing arrow" announced eight times is not the information.
 */
function flow(steps, { down = false } = {}) {
  const node = el('div', { class: `tour-flow${down ? ' down' : ''}`, role: 'list' });
  steps.forEach((step, i) => {
    if (i) node.append(el('span', { class: 'tour-to', text: down ? '↓' : '→', 'aria-hidden': 'true' }));
    const [name, note] = [].concat(step);
    node.append(el('span', { class: 'tour-box', role: 'listitem' }, [
      el('strong', { text: name }),
      note ? el('span', { class: 'tour-note', text: note }) : null,
    ]));
  });
  return node;
}

/** Several `flow`s and prose stacked, which is what most slides actually are. */
const stack = (children) => el('div', { class: 'tour-stack' }, children.filter(Boolean));

// ── Live figures ───────────────────────────────────────────────────────────────
//
// The three numbers the tour quotes off the running daemon. They are what turns the last
// slide from a brochure into a status line, and they are why `render` takes the state at
// all. Every one of them degrades to a dash: the tour is readable before either socket has
// answered, which is exactly when a new user is looking at it.

const count = (value) => (Number.isFinite(value) ? String(value) : '—');

/** How many backends are configured, and how many of them actually came up. */
function backendTally(state) {
  const backends = state.backends || [];
  return {
    configured: backends.length,
    running: backends.filter((b) => b.status === 'running').length,
  };
}

// ── The slides ─────────────────────────────────────────────────────────────────

export default [
  {
    id: 'what',
    title: 'One gateway, many servers',
    tag: 'What it is',
    render: (state) => stack([
      p(
        'A long-lived daemon that presents the union of every MCP server you use as a '
        + 'single MCP server. Clients attach to it; it owns the backends, their processes '
        + 'and their credentials.',
      ),
      flow([
        ['your client', 'Claude Desktop, Claude Code, the admin UI'],
        ['this gateway', 'one endpoint, one credential store'],
        ['N backends', 'each a stdio subprocess it owns'],
      ]),
      p(
        `Configured here: ${count(backendTally(state).configured)} server(s). `
        + 'Tools, prompts, resources and templates from all of them arrive at your client '
        + 'as one listing, each name prefixed with the server it came from.',
      ),
    ]),
  },

  {
    id: 'why',
    title: 'Why run one',
    tag: 'The case',
    render: () => stack([
      cards([
        [
          'One credential store',
          'Every token lives in one gitignored gateway.env. Each backend receives only its '
          + 'own, built from an allowlist rather than inherited from your shell.',
        ],
        [
          'Your client config stops changing',
          'Adding an integration is one entry in servers.yaml and one line in gateway.env. '
          + 'The client keeps pointing at the same endpoint forever.',
        ],
        [
          'One shared pool',
          'Three clients attached means one copy of each backend — one npx cold start in '
          + 'total, and one live state that every client and this UI observe identically.',
        ],
        [
          'A bench you can work in',
          'Invoke any tool, read any resource, fill a form from the values a server '
          + 'publishes, and hand the whole session to an agent as one document.',
        ],
      ]),
      p(
        'Rotating a credential restarts only the backend that uses it. Everything else '
        + 'keeps its process and its in-flight work.',
      ),
    ]),
  },

  {
    id: 'attach',
    title: 'Attach anything',
    tag: 'Clients',
    render: () => stack([
      p('Three ways in, on one port, at the same time.'),
      pairs([
        ['WebSocket', 'ws://host:port/mcp — the native transport, and what the admin UI uses.'],
        ['Streamable HTTP', 'The same /mcp path over POST, with GET opening the notification stream. No bridge needed.'],
        ['stdio bridge', 'mcp-gateway-connect pumps stdin↔WebSocket for clients that speak only stdio, and reconnects on its own.'],
      ]),
      code(
        'claude mcp add gateway -- mcp-gateway-connect --url ws://127.0.0.1:8765/mcp\n'
        + 'claude mcp add --transport http gateway http://127.0.0.1:8765/mcp',
      ),
      p(
        'The desktop app is the fourth door: the daemon, its backends and this UI in one '
        + 'window, with the access key minted per launch and never written down.',
      ),
    ]),
  },

  {
    id: 'architecture',
    title: 'How a call travels',
    tag: 'Architecture',
    render: () => stack([
      flow([
        ['transport', 'bind, path, access key, origin'],
        ['session', 'one connection, one method table'],
        ['router', 'undo the prefix, forward, translate back'],
        ['backend', 'one subprocess, its own environment'],
      ]),
      p(
        'Beside that path: the supervisor owns the shared backend pool and reload, the '
        + 'catalogue merges every listing into one namespaced view, and notifications relay '
        + 'backend events out to every attached client.',
      ),
      pairs([
        ['Shared, not per-client', 'One pool of backends, however many clients are attached.'],
        ['Namespaced, not flattened', 'server__tool, and mcpgw:// URIs — so two servers may publish the same name.'],
        ['Failure is local', 'A backend that dies is restarted with backoff; nothing else notices.'],
      ]),
    ]),
  },

  {
    id: 'config',
    title: 'Two files, and only two',
    tag: 'Configuration',
    render: (state) => stack([
      pairs([
        ['servers.yaml', 'Committed. What to launch, and ${NAME} references. No values.'],
        ['gateway.env', 'Gitignored, chmod 600. The values. Read-only to the whole program.'],
      ]),
      code(
        'servers:\n'
        + '  github:\n'
        + '    command: npx\n'
        + '    args: ["-y", "@modelcontextprotocol/server-github"]\n'
        + '    env:\n'
        + '      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"',
      ),
      p(
        'This gateway read: '
        + `${state.status?.config_path || 'no config yet'} and `
        + `${state.status?.env_path || 'no env file yet'}. `
        + 'Reload re-reads both without dropping a client.',
      ),
      p(
        '+ Add in the header writes servers.yaml for you — comments and ordering intact — '
        + 'and it is the only thing in the program that writes that file.',
      ),
    ]),
  },

  {
    id: 'credentials',
    title: 'What happens to a secret',
    tag: 'Credentials',
    render: () => stack([
      pairs([
        ['Never from your shell', "A backend's environment is built from an allowlist plus its own env block. Nothing leaks in."],
        ['Never in argv', '${VAR} is refused outright in command and args, because argv is world-readable through ps.'],
        ['Never in a log', 'Every known value is scrubbed from every log record before it is emitted.'],
        ['Never written back', 'No admin method returns a secret, and nothing in the program opens gateway.env for writing.'],
      ]),
      p(
        'If the file is not where your credentials live, a secrets: block names providers — '
        + 'a Python file on your machine, or an importable module — asked before the file is.',
      ),
    ]),
  },

  {
    id: 'layout',
    title: 'The layout of this page',
    tag: 'The admin UI',
    render: () => stack([
      p('Two bars that never change, and a panel between them that does.'),
      flow([
        ['header', 'name, screen, servers, + Add, theme'],
        ['the screen', 'Basics, or this one'],
        ['footer', 'log, sockets, the two files'],
      ], { down: true }),
      p('Basics is the work surface, and it is three columns you drag the edges of:'),
      pairs([
        ['Primitives', "The selected server's tools, prompts, resources and templates, filtered, each opening into a form."],
        ['Results', 'What came back, newest first — and Clipboard, which renders the whole column as one document for an agent.'],
        ['Injectable values', 'The values a server publishes for its own parameters. Pick one and it lands in the open form.'],
      ]),
      p(
        'The columns speak MCP, not an admin API: what you invoke here is byte-identical to '
        + 'what a model gets. That is the only thing that makes a test bench worth having.',
      ),
    ]),
  },

  {
    id: 'stack',
    title: 'What it is built on',
    tag: 'Technology',
    render: () => stack([
      pairs([
        ['Python 3.12+', 'asyncio throughout. Two runtime dependencies, both pinned exactly.'],
        ['websockets', 'The socket, the HTTP responses and the SSE stream — no ASGI stack, no framework.'],
        ['ruamel.yaml', 'A pure-Python YAML 1.2 safe loader, so JSON config works through the same path.'],
        ['This UI', 'Hand-written ES modules and one stylesheet. No build step, no bundler, no CDN, no font host.'],
        ['The desktop app', 'Tauri — a Rust host with a system webview, a bundled interpreter, and the key held in Rust.'],
        ['The gate', 'ruff, pytest, a jsdom suite for this page, and a docs check that every module has its sibling .md.'],
      ]),
      p('Nothing here phones home, and the page loads nothing it does not ship.'),
    ]),
  },

  {
    id: 'now',
    title: 'This gateway, right now',
    tag: 'Live',
    render: (state) => {
      const tally = backendTally(state);
      const addr = state.status?.bind;
      const attached = (path) => (state.status?.connections || []).filter((c) => c.path === path).length;
      return stack([
        pairs([
          ['Endpoint', addr ? `${addr}/mcp` : 'not listening yet'],
          ['Backends', `${count(tally.running)} running of ${count(tally.configured)} configured`],
          ['Clients', `${count(attached('/mcp'))} on /mcp, ${count(attached('/admin'))} on /admin`],
          ['Version', state.mcp?.serverInfo?.version || state.status?.version || '—'],
        ]),
        p(
          'The cards below this deck carry the rest: the files that were read, and a row per '
          + 'server saying whether it came up and why not. For the long form, see README.md, '
          + 'GET_STARTED.md and ARCHITECTURE.md in the repository.',
        ),
      ]);
    },
  },
];
