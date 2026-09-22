// The About screen: what this gateway is, and what it is running.
//
// Two halves, and they answer two different questions. The tour is the first -- a deck of
// slides saying what this thing is for, how it is put together, how it is configured and
// what it is built on -- and the cards below it are the second: the endpoint and the two
// files. A person opening the desktop app has never seen the README; a person who has been
// running the daemon for a month wants the facts. Both are on one screen because they are
// the same question asked at two distances.
//
// There is no card per server. There was, and it repeated the header: every server is
// already a pill there on every screen, with its state on the dot, its tool count on the
// badge, and its description, command, error and missing secrets in its menu. A second
// rendering of the same rows was a second thing to keep in step with the first.
//
// The deck is `tour.js` (the mechanism) and `slides.js` (what it says); this file is what
// puts them together, which is the same split `screens/basics/` has and for the same
// reason -- one screen, one directory, and the pieces inside it are only ever about it.
//
// The endpoint to point a client at and the two files it read. Every field here is already
// somewhere on the page -- in the footer, behind a hover -- and that is the point: Basics is for
// working, and reading the state of the thing off it means knowing where each fact was put.
// Nothing here asks the daemon anything of its own. It is `admin.status` and
// `admin.backends`, which the header and the footer have already fetched, laid out to be
// read in one pass -- plus `admin.secrets.keys`, fetched beside them, for the Secrets card
// a deployment with a `secrets:` block gets.
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

/** A path's `title`: which machine it is on, when that is worth saying. */
function whose(state, path) {
  if (!path || state.connection?.mode !== 'remote') return '';
  return `${path} on ${state.connection.label}`;
}

/** One `<dt>`/`<dd>` pair, or nothing at all when there is no value to show. */
/** Pin a field to one of the Gateway card's three columns. Passes `null` through. */
function at(column, node) {
  if (node) node.classList.add(`about-col-${column}`);
  return node;
}

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

  //: Three columns, each field placed in one by what kind of fact it is rather than by where
  //: the flow happens to leave it: where to connect, what is running, and what it read.
  //: The stylesheet sizes them to match -- the paths in column 3 are the long values, and
  //: column 2 holds nothing longer than a version string. See `.about-fields`.
  const fields = el('dl', { class: 'about-fields' }, [
    //: Column 1, where to connect. The two endpoints as a client would be given them, with
    //: what is attached to each -- the same count the footer pills carry, and the reason
    //: this is not just the address. MCP leads: it is the value somebody came here to copy.
    at(1, field('MCP', addr ? `${addr}/mcp` : null, `${clients('/mcp')} client(s)`)),
    at(1, field('Admin', addr ? `${addr}/admin` : null, `${clients('/admin')} client(s)`)),
    //: Only when it is not this machine. `state.connection` comes from the desktop host
    //: rather than from `admin.status`: a remote daemon answers `bind` with `127.0.0.1:8765`
    //: exactly as a local one does, so the field that looks like it says which machine you
    //: are reading is the one field that cannot.
    at(1, field(
      'Machine',
      state.connection?.mode === 'remote'
        ? `${state.connection.label} — over SSH`
        : null,
      state.connection?.mode === 'remote'
        ? `127.0.0.1:${state.connection.localPort} here → 127.0.0.1:${state.connection.remotePort} there`
        : '',
    )),
    //: Column 2, what is running. The header's title is the fallback rather than a literal,
    //: so a branded deployment reads as itself here before `/mcp` has finished its
    //: handshake. See branding.md.
    at(2, field('Name', info?.name || document.getElementById('brand-title').textContent)),
    at(2, field('Version', info?.version || status?.version)),
    at(2, field('Uptime', status ? formatDuration(status.uptime_seconds) : null)),
    //: Column 3, what it read: full paths here, where the footer has only room for the
    //: basenames. Whose files, too -- both are on the daemon's machine, which in remote mode
    //: is not the one you are looking at.
    at(3, field('Config', status?.config_path, whose(state, status?.config_path))),
    at(3, field('Env', status?.env_path, whose(state, status?.env_path))),
  ]);

  return el('section', { class: 'about-card' }, [
    el('h2', { text: 'Gateway' }),
    fields.children.length ? fields : el('p', { class: 'about-empty', text: 'Not connected yet.' }),
  ]);
}

/**
 * Where each secret resolved from -- only once a `secrets:` block has named a provider.
 *
 * With the file alone every key came from `gateway.env`, the Env field above already says
 * so, and a card repeating it per key would be noise; so no providers means no card, and
 * the screen is what it was. With a chain, "which of these came from Vault and which fell
 * through to the file" is the first question anyone asks about a token that looks stale.
 * Names and provider refs only: `admin.secrets.keys` has no value to give, by design.
 */
function secretsCard(state) {
  const { keys = [], origins = {}, providers = [] } = state.secrets || {};
  if (!providers.length) return null;

  //: The chain in the order it is asked. `gateway.env` is always the last link, which the
  //: payload does not list because it is not configurable -- so it is said here instead.
  const chain = el('p', {
    class: 'about-line',
    text: `Asked in order: ${[...providers, 'gateway.env'].join(' → ')}`,
  });

  const body = keys.length
    ? el('dl', { class: 'about-secrets' }, keys.flatMap((key) => [
      el('dt', { text: key }),
      el('dd', { text: origins[key] || 'unknown' }),
    ]))
    : el('p', { class: 'about-empty', text: 'No secrets resolved.' });

  return el('section', { class: 'about-card' }, [
    el('h2', { text: `Secrets (${keys.length})` }),
    chain,
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
      //: `secretsCard` is null without providers, and `replaceChildren` would print that.
      ...[tour.node, gatewayCard(state), secretsCard(state)].filter(Boolean),
    );
  },
};
