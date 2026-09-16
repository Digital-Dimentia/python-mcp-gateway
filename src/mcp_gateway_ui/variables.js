// The injectable values column: the vocabularies a server publishes, and what is picked
// from them.
//
// A server that publishes `zoo://animals/{id}` usually publishes `zoo://animals` beside it:
// a listing small enough to send whole, and a template for the per-member read that would
// not be. That pair is a *vocabulary* -- the set of values `id` may take -- and this column
// is those vocabularies, one group per variable, every value a button that fills the field
// it belongs in.
//
// The pairing is read off the URIs rather than guessed: a template's fixed prefix, up to
// its first `{`, is the listing's URI. That is also what keeps this column from reading the
// server's resources at large. `resources/read` is a call to a live backend -- `zoo://ticks`
// in the fixture exists precisely to prove a read can move state -- so a resource that pairs
// with no template is never fetched.
//
// ## What this column is handed
//
// Everything below runs against `port`, installed once by `app.js`. Nothing in this file
// imports the frame, reads `state` out of it, or touches the detail panel's DOM -- the same
// rule `screen_about.js` keeps, and the reason either file can be read on its own. The
// difference is that About is a renderer of what it is given, and this is a column that
// *writes into a form*, so what it is handed is bigger and worth naming:
//
//   state              the gateway state object, read-only in here.
//   ownListings(kind)  the selected server's `resources` or `templates`, each as
//                      `{name, uri}` with `name` in the backend's own spelling. One call
//                      rather than the two naming helpers, so `naming.py`'s mirror stays in
//                      the one file that already had it.
//
// `naming.js` is imported outright rather than handed over: it is pure functions over one
// entry, so there is nothing about it to inject and nothing it could reach back into.
//
// and the form port -- every way this column may touch the open form, enumerated:
//
//   controlFor(name, {strict})  the control a variable belongs in, or null.
//   putValue(control, value)    write it the way a keystroke would; why not, or null.
//   fillField(name, value)      the two above, plus the note line and the send count.
//   holdsOneValue(control)      a select or a parsing input, which cannot be shown a set.
//   markFanned(control, on)     the field wears that it is holding a set.
//   release(name, expected)     take a value back out, but only if it is still ours.
//   fieldName(control)          the form's own name for the field a control sits in.
//   updateSendLabel()           recount the calls a send would now make.
//
// `release` is the one that names a rule rather than an action. A pick dies when its group
// leaves the screen and the value it wrote has to go with it -- but a value *you* typed over
// it is yours, so the form is asked to release what it was given and nothing else.

import { templateVariables, expandTemplate } from './schema_form.js';
import { gatewayUri } from './naming.js';
import { clipboardChanged } from './clipboard.js';

const $ = (id) => document.getElementById(id);

//: The same 12-line shim every module that builds DOM carries; `screen_about.js` says why it
//: is copied rather than shared.
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

//: Everything this column is handed; see the header. Null until `install`, which `app.js`
//: calls as it loads -- before any socket has answered, so nothing in here can run against a
//: half-built frame.
let port = null;

/** Wire the column up. Once, from `app.js`. */
export function install(dependencies) {
  port = dependencies;
  $('btn-vars-refresh').addEventListener('click', () => {
    // The bodies go and the picks stay: Reread is a question about what the backends say
    // now, not a change of mind about what you were asking them.
    vocabularies.clear();
    varsNote(null);
    refreshVariables();
  });
}

//: Bodies already read, keyed by the gateway's URI for them. Kept across selections, so
//: flipping between two servers does not re-read either one's listings; the Reread button
//: and a `list_changed` are what clear it.
const vocabularies = new Map();

//: What is picked, per vocabulary: `{variable, multi, values}`. Keyed by the listing's
//: gateway URI rather than by the variable name, because two servers -- or two listings on
//: one server -- may both name their variable `id`.
const picks = new Map();

//: The vocabularies asked for, by the listing's gateway URI, in the order they were added.
//:
//: Empty to begin with, and deliberately: this column reads live backends, and a column that
//: opened every vocabulary a server publishes would spend a read on each one before knowing
//: whether anybody wanted it -- and would fill itself with groups that have nothing in them
//: yet, which reads as clutter rather than as an offer. So it starts as a menu and becomes a
//: column as you choose from it.
let opened = [];

//: The keys the last resolve produced -- the groups actually on screen. A pick outside this
//: set is a pick nothing can show you or take back: a closed vocabulary's, or one made under
//: a continent you have since changed. Either way it must not go on multiplying the fan-out.
let liveKeys = new Set();

//: How deep a chain of `narrows` is followed. A server whose listings point at each other in
//: a circle is not a case to handle gracefully, but it is one that must not spin the column.
const MAX_CHAIN_DEPTH = 4;

//: Set while `refreshVariables` is walking, because a pick in a narrowing group now calls it
//: and the walk it would re-enter is already going to see that pick.
let refreshing = false;

//: The groups the column last drew. Read only by `snapshot` below; see `renderVariables`.
let lastGroups = [];

/**
 * The vocabularies the selected server publishes, as
 * `[{variable, listing, template, uri}]` — `listing` and `template` in the backend's own
 * spelling, `uri` in the gateway's, because that is what `resources/read` takes.
 */
function vocabularyPairs() {
  const listings = new Map(port.ownListings('resources').map((r) => [r.name, r]));
  const pairs = [];
  for (const template of port.ownListings('templates')) {
    const spelling = template.name;
    const cut = spelling.indexOf('{');
    if (cut < 1) continue;
    // `zoo://animals/{id}` -> `zoo://animals`. The separator before the variable belongs to
    // the template, not to the listing's own URI.
    const prefix = spelling.slice(0, cut).replace(/[/#?&]+$/, '');
    const listing = listings.get(prefix);
    if (!listing) continue;
    const variable = templateVariables(spelling)[0];
    if (!variable) continue;
    pairs.push({ variable, listing: prefix, template: spelling, uri: listing.uri });
  }
  // One listing is one vocabulary, and therefore one group. Two templates can share a
  // fixed prefix -- `zoo://continents/{continent}/countries` and the longer one under it
  // both cut back to `zoo://continents` -- and both `vocabularies` and `picks` are keyed by
  // the listing's URI, so a second group on the same key would alias the first one's cache
  // and its picks. Keep the simplest pairing: fewest variables, then shortest, then
  // alphabetical, so the answer does not depend on listing order.
  //
  // Nothing is lost by dropping the others. A template that takes a value this listing does
  // not publish is not this listing's template; it is reached through the body of the one
  // that does, which is what `narrows` is for.
  const simpler = (a, b) => (
    templateVariables(a.template).length - templateVariables(b.template).length
    || a.template.length - b.template.length
    || a.template.localeCompare(b.template)
  );
  const simplest = new Map();
  for (const pair of pairs) {
    const held = simplest.get(pair.uri);
    if (!held || simpler(pair, held) < 0) simplest.set(pair.uri, pair);
  }
  return [...simplest.values()];
}

/**
 * The variable one group's values fill.
 *
 * Read off the body once there is one, because the body is the only thing that knows: a
 * listing names the template its values are spent on, and the variable is the first of that
 * template's the chain has not already bound. `zoo://continents/africa/countries` is spent
 * on a template naming `continent` *and* `country`, and the first of those is already
 * decided — it is in the URI — so this group is the `country` one.
 *
 * Falls back to the pairing's guess, which is all there is before the body arrives.
 */
function variableFor(group, read) {
  const bound = group.context || {};
  const spends = read?.narrows || read?.readOne;
  const named = spends && templateVariables(spends).find((name) => !(name in bound));
  return named || group.variable || null;
}

/**
 * Every group to draw, roots first and each child directly after its parent.
 *
 * A vocabulary whose body carries `narrows` does not publish its values: it publishes the
 * *template* of the listing that does, and which listing that is depends on what you picked
 * here. So this walks. A root comes from the pairing above; each deeper group is the parent
 * body's `narrows`, expanded with everything the chain has bound so far.
 *
 * The rule that makes the walk safe is the one the column already had: a listing is only
 * ever read at a URI something handed us. A root is named by a template's fixed prefix, and
 * a child by its parent's own body — so the column still never goes looking through a live
 * backend's resource space for something that might be a vocabulary.
 *
 * A child whose parent has no single pick is emitted `pending`: it is drawn, so you can see
 * that picking a continent is what will fill it, and it is neither read nor given a pick.
 */
function vocabularyGroups() {
  const groups = [];
  // Only what was asked for. A pairing nobody opened is an entry in the menu below, not a
  // group here and not a read.
  const available = new Map(vocabularyPairs().map((pair) => [pair.uri, pair]));
  opened = opened.filter((uri) => available.has(uri));
  const queue = opened.map((uri) => ({ ...available.get(uri), context: {}, depth: 0 }));

  while (queue.length) {
    const group = queue.shift();
    const read = group.pending ? null : vocabularies.get(group.uri);
    // Settled here rather than in the renderer, because the *next* link expands against it:
    // a group that thought it was still `continent` would write the country into the
    // continent's segment and read a URI nobody published.
    group.variable = variableFor(group, read);
    groups.push(group);
    if (group.pending || group.depth >= MAX_CHAIN_DEPTH || !read?.narrows) continue;

    // One pick, not several: a `narrows` buys one listing, and two continents' countries
    // merged would be a vocabulary the server never published. See `vocabularyGroup`.
    const pick = picks.get(group.uri);
    const chosen = pick && pick.values.size === 1 ? [...pick.values][0] : null;
    const child = { template: read.narrows, depth: group.depth + 1, parent: group.uri };
    if (chosen === null) {
      queue.push({ ...child, pending: 'pick', from: group.variable, context: group.context });
      continue;
    }

    const context = { ...group.context, [group.variable]: chosen };
    const unbound = templateVariables(read.narrows).filter((name) => !(name in context));
    if (unbound.length) {
      // Expanding now would leave a segment empty, which is a different URI from the one
      // meant — so the chain stops here rather than reading something nobody asked for.
      queue.push({ ...child, pending: 'unbound', from: unbound.join(', '), context });
      continue;
    }
    const listing = expandTemplate(read.narrows, context);
    queue.push({ ...child, listing, uri: gatewayUri(port.state.selected, listing), context, variable: null });
  }

  // A pick outlives its group in exactly two ways, and one rule buries both: a country
  // picked under Africa once the continent is Asia, and anything picked in a vocabulary that
  // has since been closed. Left in `picks`, either would keep counting toward the send
  // button's `×n` with nothing on screen to explain the number.
  //
  // The bodies are *not* dropped with them. Those are a cache of what a live backend said;
  // re-opening a vocabulary, or going back to a continent, should cost no second read.
  liveKeys = new Set(groups.filter((group) => !group.pending).map((group) => group.uri));
  for (const [key, pick] of [...picks.entries()]) {
    if (liveKeys.has(key)) continue;
    picks.delete(key);
    buryField(pick);
  }
  return groups;
}

/**
 * Read whatever has not been read yet, then draw.
 *
 * A loop rather than one pass, because the cascade is only discoverable one layer at a
 * time: which listing sits under `africa` is a fact in the body of `zoo://continents`, so
 * that body has to be in hand before the child is even a URI. Each turn resolves what is
 * now knowable and reads it; the walk ends when a turn finds nothing new, which is at most
 * once per level.
 */
async function refreshVariables() {
  renderVariables();
  if (port.state.mcp?.state !== 'ready' || refreshing) return;
  refreshing = true;
  try {
    for (let level = 0; level <= MAX_CHAIN_DEPTH; level += 1) {
      const unread = vocabularyGroups()
        .filter((group) => !group.pending && !vocabularies.has(group.uri));
      if (!unread.length) break;
      for (const group of unread) vocabularies.set(group.uri, { loading: true });
      renderVariables();
      await Promise.all(unread.map(async (group) => {
        try {
          const result = await port.state.mcp.request('resources/read', { uri: group.uri });
          vocabularies.set(group.uri, readVocabulary(result));
        } catch (err) {
          vocabularies.set(group.uri, { values: [], error: err.message || String(err) });
        }
      }));
    }
  } finally {
    refreshing = false;
  }
  renderVariables();
}

/**
 * The values in a `resources/read` result.
 *
 * JSON only, and deliberately: a vocabulary has to be machine-readable to be one, and
 * splitting prose on newlines would turn every text resource into a list of garbage values.
 * Three shapes are understood, in the order a server is likely to publish them.
 */
function readVocabulary(result) {
  const contents = result?.contents || [];
  const text = contents.map((c) => c.text).find((t) => typeof t === 'string');
  if (text === undefined) {
    return { values: [], error: 'This listing has no text content to read values from.' };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return { values: [], error: 'This listing is not JSON, so no values can be read from it.' };
  }
  return valuesFrom(body);
}

function valuesFrom(body) {
  // 1. A JSON Schema enum fragment — what `zoo://animals` publishes, and the one shape that
  //    carries labels *and* names its own template. Neither `readOne` nor `narrows` is a
  //    Schema keyword; they are the listing saying where one of its values is spent, and
  //    they differ in what it buys: `readOne` a member, `narrows` another listing.
  if (body && typeof body === 'object' && Array.isArray(body.enum)) {
    const names = Array.isArray(body.enumNames) ? body.enumNames : [];
    return {
      values: body.enum.map((value, i) => choice(value, names[i])),
      readOne: typeof body.readOne === 'string' ? body.readOne : null,
      narrows: typeof body.narrows === 'string' ? body.narrows : null,
    };
  }

  // 2. An array: of scalars, or of records carrying an id and something to call it.
  if (Array.isArray(body)) {
    const values = body.map((item) => {
      if (item === null || item === undefined) return null;
      if (typeof item !== 'object') return choice(item, null);
      const value = item.id ?? item.value ?? item.uri ?? item.name;
      return value === undefined ? null : choice(value, item.title ?? item.name);
    }).filter(Boolean);
    return { values };
  }

  // 3. An object keyed by the identifier — a map of id to record, which is how a server
  //    that never thought about clients tends to publish a set.
  if (body && typeof body === 'object') {
    const entries = Object.entries(body);
    return { values: entries.map(([key, value]) => choice(key, value?.name ?? value?.title)) };
  }

  return { values: [], error: 'This listing is a single JSON scalar, not a set of values.' };
}

/** One value, and the label to show for it when the label says something the value does not. */
function choice(value, label) {
  const text = String(value);
  return { value: text, label: label != null && String(label) !== text ? String(label) : null };
}

/**
 * Open one vocabulary, and whatever cascades from it.
 *
 * The read happens in `refreshVariables`, not here: this only says that somebody wants it.
 */
function openVocabulary(uri) {
  if (!opened.includes(uri)) opened.push(uri);
  varsNote(null);
  refreshVariables();
}

/**
 * Put one back in the menu, and forget what was picked in it.
 *
 * The picks go because they are a claim about what you meant, and a claim made in a group
 * that is no longer on screen is one nothing can show you or take back. The bodies stay:
 * those are a cache, and re-opening should not re-read a live backend.
 */
function closeVocabulary(uri) {
  opened = opened.filter((held) => held !== uri);
  // The picks go with it, this one's and the whole chain's: the next resolve buries every
  // key that is no longer on screen, and re-rendering is what runs it.
  renderVariables();
  applyPicks();
}

/** The menu this column starts as: every vocabulary not already open. */
function vocabularyPicker(available) {
  const closed = available.filter((pair) => !opened.includes(pair.uri));
  const select = el('select', { class: 'vocab-add' });
  select.append(el('option', {
    value: '',
    text: closed.length
      ? (opened.length ? 'Add a parameter…' : 'Choose a parameter to start…')
      : 'Every parameter is open',
    disabled: !closed.length,
  }));
  for (const pair of closed) {
    select.append(el('option', { value: pair.uri, text: `${pair.variable} — ${pair.listing}` }));
  }
  select.disabled = !closed.length;
  select.addEventListener('change', () => {
    const uri = select.value;
    // Back to the placeholder: the select is a verb here, not a statement of what is showing.
    select.value = '';
    if (uri) openVocabulary(uri);
  });
  return el('div', { class: 'vocab-picker' }, [
    el('label', { class: 'vocab-picker-label', text: 'Parameter' }),
    select,
  ]);
}

function renderVariables() {
  const host = $('variables');
  host.replaceChildren();

  if (!port.state.selected) {
    host.append(el('p', { class: 'empty', text: 'Select a server in the header.' }));
    return;
  }
  const available = vocabularyPairs();
  if (!available.length) {
    host.append(el('p', {
      class: 'empty',
      text: `${port.state.selected} publishes no listing that pairs with a template, so there are `
        + 'no values to offer.',
    }));
    return;
  }

  host.append(vocabularyPicker(available));
  const groups = vocabularyGroups();
  if (!groups.length) {
    host.append(el('p', {
      class: 'empty',
      text: 'Nothing open yet. Choose a parameter above and its values — and whatever they '
        + 'narrow — appear here.',
    }));
    return;
  }

  for (const group of groups) host.append(vocabularyGroup(group));
  port.updateSendLabel();
  // Kept for `clipboardSnapshot`, which must not call `vocabularyGroups` itself: that
  // resolver *buries* picks whose group has gone, and a snapshot is a reader. What is on
  // screen is what was drawn here, which is exactly the question the document asks.
  lastGroups = groups;
  clipboardChanged();
}

function vocabularyGroup(pair) {
  const read = vocabularies.get(pair.uri) || { loading: true };
  // A listing that names its own template overrides the pairing found by prefix: the
  // server knows where its values are spent better than the URIs do. A `narrows` says the
  // same thing about a listing rather than a member, and the group is named for the
  // variable the chain has not bound yet either way.
  const spends = read.narrows || read.readOne || pair.template;
  const bound = pair.context || {};
  // Settled by `vocabularyGroups`, which had to know it before this group's own child could
  // be expanded. Reading it off the body again here would be a second answer to one question.
  const variable = pair.variable;
  const depth = pair.depth || 0;

  const head = el('div', { class: 'vocab-head' }, [
    el('span', { class: 'vocab-name', text: variable || '…' }),
    el('span', { class: 'vocab-from', text: pair.listing || pair.template }),
  ]);
  // Only the root wears it: the groups under it are not separately closeable, because they
  // are not separately opened -- they are what this one narrowed to.
  if (!depth) {
    const close = el('button', {
      type: 'button',
      class: 'vocab-close',
      text: '×',
      title: `Put ${variable || 'this parameter'} back in the menu`,
      'aria-label': `Close ${variable || 'this parameter'}`,
    });
    close.addEventListener('click', () => closeVocabulary(pair.uri));
    head.append(close);
  }

  const group = el('div', { class: depth ? 'vocab vocab-child' : 'vocab' }, [
    head,
    el('p', { class: 'vocab-template', text: `${read.narrows ? 'narrows' : 'spent on'} ${spends}` }),
  ]);
  // Why this group holds these values and not others. Without it a countries group under a
  // continents group is just a shorter list than the one you saw a moment ago.
  const context = Object.entries(bound);
  if (context.length) {
    group.append(el('p', {
      class: 'vocab-context',
      text: context.map(([name, value]) => `${name} = ${value}`).join(' · '),
    }));
  }

  // Drawn, but neither read nor given a pick: the group is here to say that picking above
  // is what fills it, which is a different thing from a listing that came back empty.
  if (pair.pending) {
    group.classList.add('vocab-pending');
    group.append(el('p', {
      class: 'note',
      text: pair.pending === 'pick'
        ? `Pick one ${pair.from} above to narrow this.`
        : `This listing needs ${pair.from}, which nothing above it publishes.`,
    }));
    return group;
  }

  if (read.loading) {
    group.append(el('p', { class: 'note', text: 'Reading…' }));
    return group;
  }
  if (read.error) {
    group.append(el('p', { class: 'vocab-error', text: read.error }));
    if (pair.listing) group.append(el('p', { class: 'vocab-from', text: pair.listing }));
    return group;
  }
  if (!read.values.length) {
    group.append(el('p', { class: 'note', text: 'This listing is empty.' }));
    return group;
  }

  const pick = pickFor(pair.uri, variable);

  // One or many, and the switch is the whole feature: one value fills the field, several
  // fill it in turn and send the form once per value.
  //
  // Except where a value buys another *listing*. `many` means "send the open form once per
  // value", and reading a listing sends no form; merging two continents' countries would
  // invent a vocabulary the server never published, with nothing on the chip to say which
  // continent each country came from. So a narrowing group picks one, and the ambiguity
  // never arises rather than being papered over.
  if (read.narrows) {
    pick.multi = false;
  } else {
    const modes = el('div', { class: 'vocab-modes', role: 'group' }, [
      modeButton(pick, false, 'one'),
      modeButton(pick, true, 'many'),
    ]);
    // Before the close button, which stays the last thing in the row: the control that
    // removes the group should not move when the group grows a switch.
    const headRow = group.querySelector('.vocab-head');
    headRow.insertBefore(modes, headRow.querySelector('.vocab-close'));
  }

  // Radios in `one`, boxes in `many`, and the same chip around either: the switch changes
  // what picking means, and the control under your cursor says which it currently is.
  const values = el('div', { class: 'vocab-values' });
  for (const item of read.values) {
    values.append(valueChoice(pick, pair.uri, variable, item, !!read.narrows));
  }
  group.append(values);

  const count = el('span', { class: 'vocab-count' });
  const clear = el('button', { type: 'button', class: 'ghost', text: 'Clear' });
  clear.addEventListener('click', () => {
    pick.values.clear();
    fillPick(pick, { quiet: true });
    // Clearing a narrowing group unmakes what it narrowed: the groups below go back to
    // pending, and their picks go with them. That is the resolver's job, not a re-render's.
    if (read.narrows) refreshVariables();
    else renderVariables();
  });
  group.append(el('div', { class: 'vocab-foot' }, [count, clear]));

  const say = () => {
    const n = pick.values.size;
    if (!n) count.textContent = 'Nothing picked.';
    else if (read.narrows) count.textContent = `Narrowed to ${[...pick.values][0]}.`;
    else if (!pick.multi) count.textContent = `${[...pick.values][0]} is in the field.`;
    else count.textContent = `${n} picked — the form is sent ${n} time${n === 1 ? '' : 's'}.`;
    clear.disabled = !n;
  };
  pick.say = say;
  say();
  return group;
}

/**
 * Take a buried pick's value back out of the form.
 *
 * The other half of "a pick lives as long as its group is on screen". Change the continent
 * and the country picked under the old one stops existing — but the form is still holding
 * it, and a template expanding to `.../africa/countries/nepal/animals` is a read that will
 * miss. The value has to go where the pick went.
 *
 * **Only what this pick put there.** A value you typed over it is yours, and a pick dying
 * elsewhere in the column is no reason to take it away. Strictly bound, like every other
 * write: a field carrying the variable's name, or nothing.
 */
function buryField(pick) {
  port.release(pick.variable, pickDisplay([...pick.values]));
}

/** The pick state for one vocabulary, created on first sight. */
function pickFor(uri, variable) {
  let pick = picks.get(uri);
  if (!pick) {
    pick = { variable, multi: false, values: new Set(), say: () => {} };
    picks.set(uri, pick);
  }
  pick.variable = variable;   // a re-read may have moved the listing to another template
  return pick;
}

function modeButton(pick, multi, label) {
  const button = el('button', {
    type: 'button',
    class: `vocab-mode${pick.multi === multi ? ' on' : ''}`,
    text: label,
    'aria-pressed': String(pick.multi === multi),
    title: multi
      ? 'Pick several; the form is sent once per value'
      : 'Pick one; it fills the field',
  });
  button.addEventListener('click', () => {
    if (pick.multi === multi) return;
    pick.multi = multi;
    // Narrowing keeps the first pick rather than dropping the lot: `many` -> `one` after
    // picking three is a change of mind about the fan-out, not about the animals.
    if (!multi) {
      const first = [...pick.values][0];
      pick.values = new Set(first === undefined ? [] : [first]);
    }
    fillPick(pick, { quiet: true });
    renderVariables();
  });
  return button;
}

/**
 * One value, as the control the current mode calls for.
 *
 * A radio in `one` and a box in `many`, both inside the same chip. The pair is the point:
 * picking looks like picking either way, and the shape of the control is what says whether
 * this vocabulary spends one value or several.
 */
function valueChoice(pick, group, variable, item, narrows) {
  const chosen = pick.values.has(item.value);
  const box = el('input', {
    type: pick.multi ? 'checkbox' : 'radio',
    // Radios need a shared name to be one group, and the listing's URI is the one name a
    // vocabulary already has that no other vocabulary shares.
    name: `vocab:${group}`,
    checked: chosen,
  });
  const wrap = el('label', {
    class: `vocab-value${chosen ? ' on' : ''}`,
    title: narrows
      ? `Narrow what follows to ${item.value}`
      : (pick.multi
        ? `Send the form once with ${item.value}`
        : `Put ${item.value} in ${variable}`),
  }, [
    box,
    el('span', { text: item.label || item.value }),
    item.label ? el('code', { text: item.value }) : null,
  ]);

  box.addEventListener('change', () => {
    if (pick.multi) {
      if (box.checked) pick.values.add(item.value);
      else pick.values.delete(item.value);
    } else {
      // The browser has already unchecked the other radio; this is the same fact in the
      // pick.
      pick.values = new Set(box.checked ? [item.value] : []);
    }
    for (const chip of wrap.parentElement.children) {
      chip.classList.toggle('on', chip.querySelector('input').checked);
    }
    pick.say();
    // `many` writes the whole set, and only where the set can be fanned out from. `one`
    // writes the value, and may fall back to the field you were last typing in.
    if (pick.multi) fillPick(pick);
    else if (box.checked) port.fillField(variable, item.value);
    port.updateSendLabel();
    // What this value bought is another listing, and which one depends on the value — so
    // the group below has to be resolved again and read. The read is keyed by the expanded
    // URI, so coming back to a continent you already opened costs nothing.
    if (narrows) refreshVariables();
  });
  return wrap;
}

/** How a set reads in a field it does not fit in: `[axolotl, capybara]`. */
function pickDisplay(values) {
  return values.length > 1 ? `[${values.join(', ')}]` : (values[0] ?? '');
}

/**
 * Show a whole pick in the field it binds to.
 *
 * One value goes in as itself. Several go in as `[a, b]` — not a value the form will ever
 * send, and not pretending to be one: it is the fan-out, written where the fan-out will
 * happen, so the form shows what the send button's `×6` is counting. Every reader of the
 * form knows to ask what it means: the template expands one line per value, the wire
 * preview shows the first call, and the send writes the real values in one at a time.
 *
 * Strictly bound, like the fan-out itself. A set written into a field that merely had focus
 * is a set that would be sent literally, since the fan-out would not rewrite it.
 */
function fillPick(pick, { quiet = false } = {}) {
  // Every pick change comes through here, whether or not a form is open to take it -- so
  // this is the one place the clipboard has to be told about one. Debounced there.
  clipboardChanged();
  const values = [...pick.values];
  const say = quiet ? () => {} : varsNote;
  const control = port.controlFor(pick.variable, { strict: true });
  //: Nothing in the open form takes this variable -- or nothing is open yet. That is an
  //: ordinary way to work, not a mistake to report: you pick the values you want and then
  //: open the thing to spend them on. The group's own foot line already says how many are
  //: picked, and the send button counts them the moment a form that takes them is open.
  if (!control) {
    say(null);
    return;
  }
  // A select takes one of its own options, and a number input silently drops text it
  // cannot parse. Neither can hold a set, so both show the value the first call will use --
  // which of the two a control is, is the form's knowledge and not this column's.
  const oneOnly = port.holdsOneValue(control);
  say(port.putValue(control, oneOnly ? (values[0] ?? '') : pickDisplay(values)));
  port.markFanned(control, !oneOnly && values.length > 1);
}

/**
 * The picks that are still on screen, as `[key, pick]`.
 *
 * `vocabularyGroups` already drops the rest, but it only runs when the column resolves, and
 * the fan-out reads `picks` directly — so this is what keeps a pick made a moment before a
 * group closed out of a send that happens a moment after.
 */
function livePicks() {
  return [...picks.entries()].filter(([key]) => liveKeys.has(key));
}

/**
 * Write every pick into the open form. The form is new, or the values moved.
 *
 * **Every pick, not only the fanned-out ones.** This used to write `many` picks alone, on
 * the assumption that a single value had already gone into the form at the moment it was
 * clicked — true when the form was open first and the value picked second.
 *
 * A cascade reverses that order. You cannot pick a country until you have picked its
 * continent, so by the time the template that takes them is open, all three picks are
 * already made and clicking them again is exactly what nobody should have to do. The whole
 * chain is written in here instead.
 *
 * Safe because `fillPick` binds strictly: a value goes in a field that carries its name, or
 * it goes nowhere. Opening a form can therefore never scatter picks into whatever fields it
 * happened to have.
 */
function applyPicks() {
  for (const [, pick] of livePicks()) {
    if (pick.values.size) fillPick(pick, { quiet: true });
  }
  port.updateSendLabel();
}

/** The open form's fields that hold a set, as `fieldName -> values`. */
function fannedFields() {
  const fanned = new Map();
  for (const { control, values } of boundPicks()) {
    if (values.length < 2) continue;
    const field = port.fieldName(control);
    if (field) fanned.set(field, values);
  }
  return fanned;
}

/** `{id: '[a, b]'}` and `{id: ['a','b']}` -> `[{id: 'a'}, {id: 'b'}]`. */
function spread(values, fanned) {
  let combos = [values];
  for (const [name, picked] of fanned) {
    if (!(name in values)) continue;
    // The picked value is always a string; the collected one says what the field's schema
    // made of it, and the preview should not turn a number into a quoted one.
    const like = (value) => (typeof values[name] === 'number' ? Number(value) : value);
    combos = combos.flatMap((combo) => picked.map((value) => ({ ...combo, [name]: like(value) })));
  }
  return combos;
}

/** A line under the column head, or nothing. The only place this column talks back. */
function varsNote(text) {
  const line = $('vars-note');
  line.textContent = text || '';
  line.hidden = !text;
}

/** The picks that name a field in the open form, as `[{variable, control, values}]`. */
function boundPicks() {
  const bound = [];
  for (const [, pick] of livePicks()) {
    if (!pick.multi || !pick.values.size) continue;
    const control = port.controlFor(pick.variable, { strict: true });
    if (control) bound.push({ variable: pick.variable, control, values: [...pick.values] });
  }
  return bound;
}

/** Every combination of the bound picks, as `[[{control, value}, …], …]`. */
function combinations() {
  let combos = [[]];
  for (const { control, values } of boundPicks()) {
    combos = combos.flatMap((combo) => values.map((value) => [...combo, { control, value }]));
  }
  return combos.length === 1 && !combos[0].length ? [] : combos;
}

/** Every group on screen, for the clipboard document. */
export function snapshot() {
  // `lastGroups`, not a fresh resolve: `vocabularyGroups` *buries* picks whose group has
  // gone, and a snapshot is a reader. What is on screen is what was drawn.
  return lastGroups.map(snapshotGroup).filter(Boolean);
}

/** One vocabulary group: where its values came from, what they are, and what was picked. */
function snapshotGroup(group) {
  const read = vocabularies.get(group.uri) || {};
  const pick = picks.get(group.uri);
  // A group the column drew but never read is still worth reporting -- "this parameter is
  // waiting on a pick above it" is a fact about the session, and a snapshot that dropped it
  // would make the document disagree with the screen.
  const note = group.pending === 'pick'
    ? `Waiting on a pick in ${group.from} above.`
    : group.pending
      ? `Needs ${group.from}, which nothing above it publishes.`
      : read.loading ? 'Still being read.'
        : read.error ? `Could not be read: ${read.error}` : '';
  return {
    variable: group.variable || '',
    listing: group.listing || '',
    spends: read.narrows || read.readOne || group.template || '',
    narrows: !!read.narrows,
    multi: !!pick?.multi,
    context: group.context || {},
    picked: pick ? [...pick.values] : [],
    values: note ? [] : (read.values || []),
    note,
  };
}

// ── What the rest of the page may use ──────────────────────────────────────────
//
// The live bindings are the reason this is one `export` block rather than a keyword on each
// declaration: `opened`, `liveKeys` and `refreshing` are reassigned in here, `app.js`
// re-exports them straight through, and `tests/ui/` reads them to check what a cascade left
// behind. A copy taken at import time would answer the first question correctly and every
// later one wrongly.

export {
  vocabularies,
  picks,
  opened,
  liveKeys,
  refreshing,
  vocabularyPairs,
  vocabularyGroups,
  variableFor,
  valuesFrom,
  readVocabulary,
  refreshVariables,
  renderVariables,
  openVocabulary,
  closeVocabulary,
  pickFor,
  fillPick,
  applyPicks,
  livePicks,
  buryField,
  boundPicks,
  combinations,
  fannedFields,
  spread,
  varsNote,
};
