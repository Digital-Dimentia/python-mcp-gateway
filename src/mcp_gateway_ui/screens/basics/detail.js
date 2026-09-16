// The detail panel: the form a primitive opens into, and the button that sends it.
//
// One panel for three kinds. A tool is a JSON Schema, a prompt is a list of arguments and a
// template is a set of `{variable}` segments, and all three end up as the same thing here: a
// form, a wire preview, and a send that puts what came back in the Results column. What they
// share is not cosmetic -- every field is tagged `data-field`, which is what lets the
// injectable values column fill any of them without knowing which kind it is looking at.
//
// ## Two seams, in opposite directions
//
// **What this panel is handed** is `port`, installed by `app.js`: the state,
// `selectionChanged` so the list can mark what is open, and `pushCard` to put a result on
// the right. Nothing here imports the frame -- the same one-way rule the other modules keep,
// and `tests/test_webui.py` checks it. `naming.js` is imported outright instead, because
// pure functions over one entry have nothing to inject and nothing to reach back into.
//
// **What this panel hands out** is `formPort`: the eight functions the injectable values
// column needs in order to write a picked value into whatever form is open. They are
// exported from here rather than implemented in `app.js` because they *are* the form -- a
// frame that knew how to put a value in a field would be a frame that owned the panel. So
// `app.js` composes the column's port out of its own catalogue helpers and this object,
// which is the whole of its involvement in either.
//
// The one thing that crosses in both directions is the fan-out. A picked set lives in
// `variables.js`; sending the form once per value happens here. `fanOut` asks the column
// what the combinations are, writes each one in as an `input` event, and collects and sends
// the form exactly as a click would -- so a fanned-out call is byte-identical to the one you
// would have made by typing the value yourself, which is the same reason the middle column
// speaks MCP rather than `admin.*`.

import { RpcError } from '../../rpc.js';
import { buildForm, buildPromptForm, templateVariables, expandTemplate } from '../../schema_form.js';
import {
  renderToolResult, renderPromptResult, renderResourceResult, renderError, pretty,
} from '../../render.js';
import { localName, itemId } from '../../naming.js';
import * as variables from './variables.js';

const $ = (id) => document.getElementById(id);

//: The same 12-line shim every module that builds DOM carries; `screens/about.js` says why it
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

//: What this panel is handed; see the header. Null until `install`, which `app.js` calls as
//: it loads.
let port = null;

/** Wire the panel up. Once, from `app.js`. */
export function install(dependencies) {
  port = dependencies;
  //: The field you were last typing in, which `controlFor` falls back to. On the panel
  //: rather than on each form, because a form is replaced every time something is opened
  //: and this outlives that.
  $('detail').addEventListener('focusin', (event) => {
    const control = event.target.closest('input, select, textarea');
    lastFocused = control && !control.readOnly ? control : lastFocused;
  });
}

// ── Fanning a form out over several values ─────────────────────────────────────
//
// Nothing here reaches inside a form. Each combination is written into the controls as an
// `input` event and the form is then collected and sent exactly as a click would collect
// and send it — so a fanned-out call is byte-identical to the one you would have made by
// typing the value yourself, which is the same reason the middle column speaks MCP.

//: How many calls a fan-out may start before it asks. Not a limit, a speed bump: six
//: animals crossed with four sizes is twenty-four calls at a live backend, and the person
//: who meant that should say so once.
const FAN_OUT_ASKS_ABOVE = 8;

/** Run `once` for each combination, or exactly once when nothing is fanned out. */
async function fanOut(once) {
  const combos = variables.combinations();
  if (!combos.length) return once();
  if (combos.length > FAN_OUT_ASKS_ABOVE
      && !confirm(`This sends the form ${combos.length} times. Go ahead?`)) return;
  for (const combo of combos) {
    for (const { control, value } of combo) putValue(control, value);
    await once();                 // sequential: the result cards land in the order picked
  }
  // The form is left holding the last combination otherwise, which reads as though the
  // picks had collapsed to whatever went out last.
  variables.applyPicks();
}

/** Name the send button so a fan-out can say how many calls it is. */
function registerSend(button, base) {
  sendControl = { button, base };
  updateSendLabel();
}

function setSendBase(base) {
  if (sendControl) sendControl.base = base;
  updateSendLabel();
}

function updateSendLabel() {
  if (!sendControl || !$('detail').contains(sendControl.button)) return;
  const n = variables.combinations().length;
  sendControl.button.textContent = n > 1 ? `${sendControl.base} ×${n}` : sendControl.base;
}

// ── The form port: everything the variables column may do to a form ───────────
//
// Everything the variables column may do to the open form, and nothing else does it: these
// functions are handed to `variables.install` as its port, so the column never reaches into
// the detail panel itself. See the header of `variables.js` for the list and for why the
// list is written down at all.

//: Input types that parse what they are given, and so cannot be shown a `[a, b]` set: the
//: browser drops the text and leaves the field empty, without saying so.
const PARSED_INPUTS = new Set([
  'checkbox', 'radio', 'number', 'range', 'date', 'time', 'datetime-local', 'month', 'week',
  'color',
]);

//: The control in the detail panel that last had focus. The fallback for a value whose
//: variable names no field in the open form: the field you were last typing in is a better
//: guess than nothing, and it is the only sane target for a raw-JSON form.
let lastFocused = null;

//: The open form's send button and the word it wears when nothing is fanned out. Held here
//: so a pick made in this column can show, on the button, how many calls it just bought.
let sendControl = null;

/**
 * The control in the open form that `name` belongs in, or null.
 *
 * The form may be a tool's, a prompt's or a template's — they all tag their fields with
 * `data-field`, so one lookup covers the three. The loosening below stops at the point
 * where a wrong guess would be worse than none: an exact name, then the same name spelt in
 * another case or with other separators, then a field whose name ends in it (`animalId` for
 * `id`), then whatever you were last typing in.
 */
function controlFor(name, { strict = false } = {}) {
  const detail = $('detail');
  const fields = [...detail.querySelectorAll('[data-field]')];
  const norm = (text) => String(text).toLowerCase().replace(/[^a-z0-9]/g, '');
  const target = fields.find((f) => f.dataset.field === name)
    || fields.find((f) => norm(f.dataset.field) === norm(name))
    || fields.find((f) => norm(f.dataset.field).endsWith(norm(name)) && norm(name).length > 1);

  // `strict` drops the last-focused fallback. Filling a field you were just typing in is a
  // helpful guess; *fanning a form out* into it because a box is ticked somewhere is not.
  const fallback = !strict && detail.contains(lastFocused) ? lastFocused : null;
  return target?.querySelector('input:not([readonly]), select, textarea') || fallback;
}

/** Put `value` in `control`, the way a keystroke would. Returns why not, or null. */
function putValue(control, value) {
  if (control.tagName === 'SELECT') {
    // Matched on either spelling. A boolean or a `null` select wears its value directly; a
    // schema `enum` wears the choice's *index*, because `{"enum": [0, 1, 3, 5]}` has to
    // come back as the number 3 rather than the string "3" -- so `schema_form.js` carries
    // the choice's own spelling on `dataset.value` for exactly this comparison. Matching
    // only `option.value` rejected every enum field, about values it plainly offered.
    const option = [...control.options]
      .find((candidate) => candidate.value === value || candidate.dataset.value === value);
    if (!option) {
      return `${value} is not one of the choices this field offers.`;
    }
    control.value = option.value;
  } else if (control.type === 'checkbox') {
    return 'That field is a checkbox, so a value cannot be put in it.';
  } else {
    control.value = value;
  }

  // The forms recompute their preview and their problems off `input`, so the value has to
  // arrive the way a keystroke would rather than by assignment alone.
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.dispatchEvent(new Event('change', { bubbles: true }));

  const field = control.closest('.field') || control;
  // Whatever this field was holding, it is holding a plain value now. `fillPick` puts the
  // mark back when what it just wrote is a set.
  field.classList.remove('field-fanned');
  field.classList.remove('field-filled');
  void field.offsetWidth;               // restart the animation on a second pick
  field.classList.add('field-filled');
  field.scrollIntoView({ block: 'nearest' });
  return null;
}

/** The field wears the fact that it is holding a set rather than a value. */
function markFanned(control, fanned) {
  control.closest('.field')?.classList.toggle('field-fanned', fanned);
}

/**
 * Whether a control can only ever show one value.
 *
 * A select takes one of its own options and a parsing input silently drops text it cannot
 * read, so neither can be shown the `[a, b]` a fan-out writes. Which controls those are is
 * the form's own knowledge, which is why the column asks rather than deciding.
 */
function holdsOneValue(control) {
  return control.tagName === 'SELECT' || PARSED_INPUTS.has(control.type);
}

/** The form's own name for the field a control sits in, or null. */
function fieldName(control) {
  return control.closest('[data-field]')?.dataset.field || null;
}

/**
 * Take a value back out of the form — but only the one the column put there.
 *
 * The other half of "a pick lives as long as its group is on screen". Change the continent
 * and the country picked under the old one stops existing, while the form is still holding
 * it, and a template expanding to `.../africa/countries/nepal/animals` is a read that will
 * miss. So the value has to go where the pick went.
 *
 * **Only what that pick wrote.** A value you typed over it is yours, and a pick dying
 * elsewhere in the column is no reason to take it away — hence `expected`, which is what the
 * column last wrote and what this refuses to act without. Strictly bound, like every other
 * write: a field carrying the variable's name, or nothing.
 */
function release(name, expected) {
  const control = controlFor(name, { strict: true });
  if (!control || control.value !== expected) return;
  control.value = '';
  // As a keystroke would, so the expansion line and the problems recompute — the whole
  // point is that the form stops claiming a value it no longer has.
  control.dispatchEvent(new Event('input', { bubbles: true }));
  control.dispatchEvent(new Event('change', { bubbles: true }));
  markFanned(control, false);
}

/** Put `value` in the open form's `name` field, if the open form has one. */
function fillField(name, value) {
  const control = controlFor(name);
  //: Nowhere to put it, which is not news -- the same reasoning as `fillPick`. Picking a
  //: value before opening the thing that takes it is how the column is meant to be used,
  //: and a line of complaint under the heading every time you do it is the column talking
  //: over the work. What `putValue` reports below is different: those are cases where there
  //: *is* a field and the value cannot go in it, which is worth a word.
  if (!control) {
    variables.varsNote(null);
    return;
  }
  variables.varsNote(putValue(control, value));
  updateSendLabel();
}

// ── Suggestions for one argument ───────────────────────────────────────────────
//
// `completion/complete` is the protocol's own answer to the question the variables column
// answers by hand: what may go in this box? The two are worth having side by side. The
// column is how a *person* browses a vocabulary — every value visible, several pickable,
// the fan-out counted on the button. This is how a box gets filled while you are typing in
// it, which is the thing a model does and a person does more often.
//
// It is a `<datalist>` rather than a `<select>` on purpose. `putValue` refuses a value that
// is not among a select's options, so a constraining control here would break the column's
// own fill — and the `[a, b]` a fan-out writes into a field is not a value any server would
// ever suggest. A datalist offers without constraining, which is what a *suggestion* is.

//: How long after a keystroke the suggestion is asked for. Long enough that typing a word
//: is one request rather than five, short enough not to arrive after you have stopped.
const COMPLETE_DEBOUNCE_MS = 180;

//: Every completion request in order, so a slow answer cannot overwrite a newer one. A
//: WebSocket has no `AbortController`, so the sequence number is the whole mechanism.
let completionSeq = 0;

//: Ids for the datalists, counted apart from the requests: sharing one counter would let
//: opening a form cancel a request that was already in flight for a different field.
let completionLists = 0;

function completionsEnabled() {
  return !!(port.state.mcp?.capabilities || {}).completions;
}

/**
 * Offer server-suggested values in `input`, for the argument `name` of `ref`.
 *
 * `siblings()` is what makes this a cascade rather than a list: it returns the arguments of
 * the same form that are already filled in, and the server is free to narrow by them — the
 * countries of the continent above, rather than every country there is.
 *
 * Nothing here can fail loudly. A gateway that does not know the method, a backend that is
 * down, a request that times out: all of them clear the list and say so in the tooltip. A
 * suggestion that broke the form it was helping with would be worse than no suggestion.
 */
function attachCompletions(input, { ref, name, siblings }) {
  // Called after the input is in its field, because a `<datalist>` has to be *somewhere* in
  // the document for the browser to find it by id, and an input that is not yet in one has
  // nowhere to put it.
  if (!completionsEnabled() || !input.parentElement) return;
  const list = el('datalist', { id: `completions-${(completionLists += 1)}` });
  input.setAttribute('list', list.id);
  input.setAttribute('autocomplete', 'off');
  input.parentElement.append(list);

  let timer = null;
  const ask = async () => {
    const value = input.value;
    // A field holding a fan-out holds `[a, b]`, which is not a prefix of anything.
    if (value.startsWith('[')) return;
    const seq = (completionSeq += 1);
    try {
      const result = await port.state.mcp.request('completion/complete', {
        ref,
        argument: { name, value },
        context: { arguments: siblings() },
      });
      if (seq !== completionSeq) return;      // a later keystroke already asked
      const values = result?.completion?.values || [];
      list.replaceChildren(...values.map((v) => el('option', { value: String(v) })));
      const more = result?.completion?.hasMore;
      input.title = more ? `${values.length} of ${result.completion.total} suggestions` : '';
    } catch (err) {
      if (seq !== completionSeq) return;
      list.replaceChildren();
      input.title = `No suggestions: ${err.message || err}`;
    }
  };
  const soon = () => {
    clearTimeout(timer);
    timer = setTimeout(ask, COMPLETE_DEBOUNCE_MS);
  };
  // On focus as well as on input, because the useful moment is the one before anything has
  // been typed: an empty box is where a person most wants to be told what goes in it.
  input.addEventListener('focus', soon);
  input.addEventListener('input', soon);
}

/** Everything filled in beside `name`, as `context.arguments` wants it. */
function siblingValues(values, name) {
  const out = {};
  for (const [key, value] of Object.entries(values)) {
    if (key === name || value === '' || value === undefined || value === null) continue;
    // A field holding a fan-out is holding several values; none of them is *the* one that
    // narrows this, so it says nothing here rather than the wrong thing.
    if (typeof value === 'string' && value.startsWith('[')) continue;
    out[key] = String(value);
  }
  return out;
}

//: How many expansions a template's `Expands to` line shows before it counts the rest.
const EXPANSIONS_SHOWN = 6;

// ── The detail panel: a form, and the button that sends it ─────────────────────

/**
 * Empty the panel.
 *
 * `sendControl` goes with it, which is the part a bare `replaceChildren` from outside used
 * to miss: the button it names has just left the document, and a send count recomputed
 * against a detached node is a number about nothing.
 */
export function clear() {
  $('detail').replaceChildren();
  sendControl = null;
}

function openItem(kind, entry) {
  port.state.item = { kind, entry };
  port.selectionChanged();
  const detail = $('detail');
  detail.replaceChildren();
  // The old form's send button has just left the page; whatever replaces it registers
  // itself below.
  sendControl = null;

  const head = el('div', { class: 'detail-head' }, [
    el('h3', { text: localName(kind, entry) }),
    el('code', { class: 'detail-id', text: itemId(entry) }),
  ]);
  detail.append(head);
  if (entry.description) detail.append(el('p', { class: 'detail-desc', text: entry.description }));

  // The form is built first and filled second: the picks are written into whatever fields
  // it turns out to have, exactly as they would be if you had picked them now.
  if (kind === 'tools') renderToolDetail(detail, entry);
  else if (kind === 'prompts') renderPromptDetail(detail, entry);
  else renderResourceDetail(detail, entry, kind === 'templates');
  variables.applyPicks();
}

function renderToolDetail(detail, entry) {
  const preview = el('pre', { class: 'block preview' }, [el('code', { text: '{}' })]);
  const problems = el('ul', { class: 'problems', hidden: true });

  const form = buildForm(entry.inputSchema, { onChange: update });
  detail.append(form.element);

  if (entry.outputSchema) {
    detail.append(el('details', { class: 'raw' }, [
      el('summary', { text: 'outputSchema' }),
      el('pre', { class: 'block' }, [el('code', { text: pretty(entry.outputSchema) })]),
    ]));
  }

  const send = el('button', { type: 'button', class: 'primary', text: 'Call tool' });
  const bar = el('div', { class: 'detail-actions' }, [
    send,
    el('details', { class: 'raw preview-wrap' }, [el('summary', { text: 'Wire payload' }), preview]),
  ]);
  detail.append(problems, bar);

  function update() {
    try {
      // The first of the calls, when a field is holding a set: the payload of a fan-out is
      // n payloads, and the first one is the only honest single thing to show.
      const [args] = variables.spread(form.collect(), variables.fannedFields());
      preview.firstChild.textContent = pretty({
        method: 'tools/call', params: { name: entry.name, arguments: args },
      });
      const found = form.problems();
      problems.replaceChildren(...found.map((text) => el('li', { text })));
      problems.hidden = found.length === 0;
      // Advice, not a gate. The button stays live: the gateway and the backend are the
      // authority on what is acceptable, and a form that refused to send would be
      // pretending to be one.
      setSendBase(found.length ? 'Call anyway' : 'Call tool');
    } catch (err) {
      preview.firstChild.textContent = String(err.message);
      problems.replaceChildren(el('li', { text: err.message }));
      problems.hidden = false;
      setSendBase('Call tool');
    }
  }
  registerSend(send, 'Call tool');
  update();

  // Collected inside the loop, not outside it: a fan-out writes each value into the form
  // and this reads the form back, so every call is the one the visible form describes.
  const once = async () => {
    let args;
    try {
      args = form.collect();
    } catch (err) {
      problems.replaceChildren(el('li', { text: err.message }));
      problems.hidden = false;
      return;
    }
    await invoke({
      title: entry.name,
      subtitle: 'tools/call',
      method: 'tools/call',
      params: { name: entry.name, arguments: args },
      render: renderToolResult,
      // A tool-level failure arrives as a *successful* result carrying isError, which is
      // MCP's contract. The card is marked failed so it reads as one, without pretending
      // the JSON-RPC call failed.
      failedIf: (result) => !!result.isError,
    }, send);
  };
  send.addEventListener('click', () => fanOut(once));
}

function renderPromptDetail(detail, entry) {
  const form = buildPromptForm(entry.arguments, { onChange: () => {} });
  detail.append(form.element);
  // The namespaced name, which the gateway splits: a prompt ref is routed exactly the way
  // `prompts/get` is. `collect()` already drops the empty fields, so what is left is what
  // has actually been decided — the cascade's context, without having to say so.
  for (const { name, input } of form.fields || []) {
    attachCompletions(input, {
      ref: { type: 'ref/prompt', name: entry.name },
      name,
      siblings: () => siblingValues(form.collect(), name),
    });
  }
  const send = el('button', { type: 'button', class: 'primary', text: 'Get prompt' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  registerSend(send, 'Get prompt');
  send.addEventListener('click', () => fanOut(() => invoke({
    title: entry.name,
    subtitle: 'prompts/get',
    method: 'prompts/get',
    // Read per call, so a fan-out sends the arguments it just wrote rather than the first
    // set it collected.
    params: { name: entry.name, arguments: form.collect() },
    render: renderPromptResult,
  }, send)));
}

function renderResourceDetail(detail, entry, isTemplate) {
  const meta = [entry.mimeType, entry.size ? `${entry.size} B` : null].filter(Boolean).join(' · ');
  if (meta) detail.append(el('p', { class: 'detail-desc', text: meta }));

  let uriOf = () => entry.uri;

  if (isTemplate) {
    const names = templateVariables(entry.uriTemplate);
    const inputs = new Map();
    const wrap = el('div', { class: 'fields' });
    const resolved = el('code', { class: 'detail-id expansions' });
    const valuesNow = () => {
      const values = {};
      for (const [name, input] of inputs) values[name] = input.value;
      return values;
    };
    // A field holding `[a, b]` expands to a line per value rather than to one URI with a
    // bracket in it: what a fan-out is about to read is what this should show.
    const refresh = () => {
      const combos = variables.spread(valuesNow(), variables.fannedFields());
      const shown = combos.slice(0, EXPANSIONS_SHOWN)
        .map((values) => expandTemplate(entry.uriTemplate, values));
      if (combos.length > shown.length) shown.push(`…and ${combos.length - shown.length} more`);
      resolved.textContent = shown.join('\n');
    };
    for (const name of names) {
      const input = el('input', { type: 'text', spellcheck: false });
      input.addEventListener('input', refresh);
      inputs.set(name, input);
      wrap.append(el('div', { class: 'field field-string', dataset: { field: name } }, [
        el('label', { class: 'field-label' }, [el('span', { class: 'field-name', text: name })]),
        input,
      ]));
      // The gateway's own unexpanded spelling: `ref/resource` names the *template*, which
      // is exactly what `router.complete` decodes back into the backend's. Every other
      // field of this same template is the context, which is what makes `{continent}`
      // narrow `{country}` rather than the two being filled in independently.
      attachCompletions(input, {
        ref: { type: 'ref/resource', uri: entry.uriTemplate },
        name,
        siblings: () => siblingValues(valuesNow(), name),
      });
    }
    if (!names.length) wrap.append(el('p', { class: 'note', text: 'This template names no variables.' }));
    detail.append(wrap, el('p', { class: 'note' }, [el('span', { text: 'Expands to ' }), resolved]));
    refresh();
    // Expanded here rather than sent as a template: `resources/read` takes a URI, and the
    // expansion is the client's job in MCP exactly as it is in RFC 6570. Read off the
    // inputs rather than off the line above, which may be showing six of them.
    uriOf = () => expandTemplate(entry.uriTemplate, valuesNow());
  }

  const send = el('button', { type: 'button', class: 'primary', text: 'Read resource' });
  detail.append(el('div', { class: 'detail-actions' }, [send]));
  registerSend(send, 'Read resource');
  // `uriOf` is read per call: a template fanned out over six ids expands six times.
  send.addEventListener('click', () => fanOut(() => invoke({
    title: localName(isTemplate ? 'templates' : 'resources', entry),
    subtitle: 'resources/read',
    method: 'resources/read',
    params: { uri: uriOf() },
    render: renderResourceResult,
  }, send)));
}

/** Send one MCP request and put the answer on the right. */
async function invoke({ title, subtitle, method, params, render, failedIf }, button) {
  const started = performance.now();
  if (button) button.disabled = true;
  try {
    const result = await port.state.mcp.request(method, params);
    port.pushCard({
      title,
      subtitle,
      request: { method, params },
      body: render(result),
      elapsedMs: performance.now() - started,
      raw: result,
      failed: failedIf ? failedIf(result) : false,
    });
  } catch (err) {
    const error = err instanceof RpcError
      ? { code: err.code, message: err.message, data: err.data }
      : { code: -32000, message: String(err.message || err), data: null };
    port.pushCard({
      title,
      subtitle,
      request: { method, params },
      body: renderError(error),
      elapsedMs: performance.now() - started,
      raw: { error },
      failed: true,
    });
  } finally {
    if (button) button.disabled = false;
  }
}

// ── What the rest of the page may use ──────────────────────────────────────────

export { openItem };

/**
 * Everything the injectable values column may do to an open form.
 *
 * Handed to `variables.install` by `app.js`, which spreads it into the port it composes.
 * Gathered into one object rather than exported loose because the *list* is the contract --
 * see the header of `variables.js`, where each of these is written out with what it is for.
 */
export const formPort = {
  controlFor,
  putValue,
  fillField,
  holdsOneValue,
  markFanned,
  release,
  fieldName,
  updateSendLabel,
};

//: The test seam's share, re-exported by `app.js` so the suites keep reading one namespace.
export { controlFor, putValue, fillField };
