// The About screen: what this gateway is, and what it is running.
//
// Two halves, and they answer two different questions. The tour is the first -- a deck of
// slides saying what this thing is for, how it is put together, how it is configured and
// what it is built on -- and the cards below it are the second: the endpoint, the two files,
// and a row per configured server. A person opening the desktop app has never seen the
// README; a person who has been running the daemon for a month wants the rows. Both are on
// one screen because they are the same question asked at two distances.
//
// The deck is `tour.js` (the mechanism) and `slides.js` (what it says); this file is what
// puts them together, which is the same split `screens/basics/` has and for the same
// reason -- one screen, one directory, and the pieces inside it are only ever about it.
//
// The endpoint to point a client at, the two files it read, and one row per configured
// server saying whether it came up. Every field here is already somewhere on the page -- in
// the footer, in a server's menu, behind a hover -- and that is the point: Basics is for
// working, and reading the state of the thing off it means knowing where each fact was put.
// Nothing here asks the daemon anything of its own. It is `admin.status` and
// `admin.backends`, which the header and the footer have already fetched, laid out to be
// read in one pass.
//
// ## The screen contract
//
// A screen module default-exports one object, and `app.js` keeps a registry of them keyed
// by `id`. That is the whole interface:
//
//   id        the `<option>` value in the header's selector, the section's `data-screen`,
//             and the name the stylesheet and `<html data-screen>` speak in. One string,
//             four places, and the registry is what makes them agree.
//   refresh   redraw from the gateway's state. Called when the screen is shown, and again
//             whenever the payloads move while it is the screen on the glass -- never while
//             it is hidden, because keeping a page nobody is looking at up to date is work
//             nobody is looking at.
//   show      anything that can only be done once the screen is actually laid out. Optional,
//             and About has none; Basics uses it, because a column cannot be measured while
//             its screen is displaying `none`.
//
// **`refresh` is handed the state rather than importing it.** `app.js` imports this module,
// so importing `state` back out of `app.js` would be a cycle -- and, worse, it would make a
// screen a thing that reaches across a file boundary into the frame's variables. A screen is
// a renderer of what it is given. That is what makes adding one an edit to no existing file
// except the registry, and it is the seam python-mcp-gateway-sqo has to hold to when the
// much larger Basics screen comes out of `app.js` behind the same contract.

import { formatDuration } from '../../format.js';
//: `el` comes with it. Everywhere else in this UI that 12-line DOM shim is copied rather
//: than shared -- it has no decisions in it, so it cannot drift, and one more module is one
//: more asset and one more GET. Neither half of that holds here: `tour.js` is fetched by
//: this screen whatever happens, so importing it costs nothing at all.
import { deck, el } from './tour.js';
import slides from './slides.js';

/** One `<dt>`/`<dd>` pair, or nothing at all when there is no value to show. */
function field(label, value, title = '') {
  if (value === undefined || value === null || value === '') return null;
  return el('div', { class: 'about-field' }, [
    el('dt', { text: label }),
    el('dd', { text: String(value), title: title || '' }),
  ]);
}

/** The gateway itself: what it is, where it listens, and what it read to get there. */
function gatewayCard(state) {
  const status = state.status;
  const info = state.mcp?.serverInfo;
  const addr = status?.bind || '';
  const clients = (path) => (status?.connections || []).filter((c) => c.path === path).length;

  const fields = el('dl', { class: 'about-fields' }, [
    //: The header's title is the fallback rather than a literal, so a branded deployment
    //: reads as itself here before `/mcp` has finished its handshake. See branding.md.
    field('Name', info?.name || document.getElementById('brand-title').textContent),
    field('Version', info?.version || status?.version),
    field('Uptime', status ? formatDuration(status.uptime_seconds) : null),
    //: The two endpoints as a client would be given them, with what is attached to each --
    //: the same count the footer pills carry, and the reason this is not just the address.
    field('MCP', addr ? `${addr}/mcp` : null, `${clients('/mcp')} client(s)`),
    field('Admin', addr ? `${addr}/admin` : null, `${clients('/admin')} client(s)`),
    //: Full paths here, where the footer has only room for the basenames.
    field('Config', status?.config_path),
    field('Env', status?.env_path),
  ]);

  return el('section', { class: 'about-card' }, [
    el('h2', { text: 'Gateway' }),
    fields.children.length ? fields : el('p', { class: 'about-empty', text: 'Not connected yet.' }),
  ]);
}

/** One server: the indicator the header uses, what it is, and why it is not running. */
function serverRow(state, backend) {
  const name = backend.name;
  const missing = state.missing[name] || [];
  const spec = state.config.servers?.[name] || {};
  const command = [backend.command, ...(backend.args || [])].filter(Boolean).join(' ');

  const label = el('span', { class: 'about-name', text: name });
  const description = backend.description || spec.description;
  if (description) label.append(el('span', { class: 'about-desc', text: description }));

  const row = el('div', {
    class: `about-server${backend.enabled ? '' : ' off'}`,
  }, [
    el('span', { class: `dot dot-${backend.status}` }),
    label,
    el('span', { class: 'about-state', text: backend.enabled ? backend.status : 'disabled' }),
  ]);

  if (command) row.append(el('p', { class: 'about-line', text: command, title: command }));

  //: Two reasons a server is not answering, and they want different things done about them.
  //: A missing secret is a line to add to `gateway.env`; an error is what the process said.
  if (missing.length) {
    row.append(el('p', {
      class: 'about-line warn',
      text: `missing ${missing.join(', ')}`,
      title: `Not in the env file: ${missing.join(', ')}`,
    }));
  }
  if (backend.error) row.append(el('p', { class: 'about-line bad', text: backend.error }));

  return row;
}

/** The configured servers, in the order the catalogue lists them. */
function serversCard(state) {
  const body = state.backends.length
    ? el('div', { class: 'about-servers' }, state.backends.map((b) => serverRow(state, b)))
    : el('p', { class: 'about-empty', text: 'No servers configured.' });

  return el('section', { class: 'about-card' }, [
    el('h2', { text: `Servers (${state.backends.length})` }),
    body,
  ]);
}

//: Built once, on the first refresh, and kept. The deck holds which slide is showing, and
//: About repaints whenever a payload moves -- so a deck rebuilt on every refresh would jump
//: back to slide 1 the moment a backend changed state, which is precisely when somebody is
//: watching. `replaceChildren` below re-appends this same node, so nothing in it is lost.
let tour = null;

export default {
  id: 'about',

  //: Rebuilt whole rather than patched. It is two cards off two payloads that arrive
  //: together, and nothing in here holds focus or a scroll position worth preserving --
  //: which is exactly the condition under which replacing the subtree is the simple
  //: version rather than the lazy one.
  refresh(state) {
    if (!tour) tour = deck(slides);
    //: The live figures three slides quote, redrawn -- not the deck, and not the position.
    tour.update(state);
    document.getElementById('about').replaceChildren(
      tour.node, gatewayCard(state), serversCard(state),
    );
  },
};
