// The results column: what came back, one card per call.
//
// Appended rather than prepended, so the column reads in the order the calls were made, and
// a card carries its own request beside its answer -- the middle column is the evidence of a
// session, not a log of it.
//
// ## Handed nothing
//
// Every other piece of the Basics screen takes a port, because every other piece needs
// something: the state, a form to write into, a panel to open. This one needs neither the
// gateway nor the frame. A card is built out of what the caller passes, `render.js` draws
// it, and the only thing this module tells anyone is that the column changed, which
// `clipboard.js` already exports a function for. So there is no `install` here, and adding
// one to match the others would be ceremony rather than symmetry.

import { resultCard } from '../../render.js';
import { clipboardChanged } from '../../clipboard.js';

const $ = (id) => document.getElementById(id);

//: The same 12-line shim every module that builds DOM carries; `screens/about/tour.js` says why it
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

//: What each result card is about, keyed by the card itself. A `WeakMap` rather than a list
//: because the column *is* the list: deleting a card drops its record with it, and there is
//: no second structure to drift out of step with what is on screen.
const resultRecords = new WeakMap();

const RESULTS_EMPTY = 'Invoke a tool, get a prompt, or read a resource.';

/** Put the column back to its empty state. One definition, three callers. */
function clearResults() {
  $('results').replaceChildren(el('p', { class: 'empty', text: RESULTS_EMPTY }));
}

function pushCard(options) {
  const results = $('results');
  const empty = results.querySelector('.empty');
  if (empty) empty.remove();
  // A card deletes itself; what it cannot know is that it was the last one, and a column
  // left with nothing in it at all reads as broken rather than as empty.
  const card = resultCard({
    ...options,
    onRemove: () => {
      // An HTML panel's body holds a live `MessagePort` to a framed document. Deleting the
      // card takes the frame off the page; without this the port would stay open and a
      // panel nobody can see could still be answering. Every other body ignores it.
      options.body?.closePanel?.();
      if (!results.querySelector('.card')) clearResults();
      clipboardChanged();
    },
  });
  // What the card is *about*, kept beside the card rather than in a list of its own. The
  // column is the list: a card that is deleted takes its record with it, and there is no
  // second structure to fall out of step with what is on screen. See `snapshot` below.
  resultRecords.set(card, {
    title: options.title,
    subtitle: options.subtitle || '',
    method: options.request?.method || '',
    params: options.request?.params ?? {},
    raw: options.raw,
    failed: !!options.failed,
    elapsed_ms: options.elapsedMs,
    at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
  });
  results.append(card);
  // Appended rather than prepended, so the column reads in the order the calls were made
  // -- and then scrolled, because an answer below the fold is an answer nobody saw. The
  // scroll is unconditional on purpose: a card arrives because a person just pressed Send,
  // which is not the situation where being left where you were is the kindness. The log
  // pane pins itself to the bottom the same way, for a different reason.
  results.scrollTop = results.scrollHeight;
  clipboardChanged();
}

$('btn-clear-results').addEventListener('click', () => { clearResults(); clipboardChanged(); });

/**
 * Every card on screen, oldest first, for the clipboard document.
 *
 * Read off the DOM rather than off a list of its own, which is the whole point of the
 * `WeakMap`: what is in the column *is* the answer, and a card someone deleted is gone from
 * both at once.
 */
export function snapshot() {
  return [...$('results').querySelectorAll('.card')]
    .map((card) => resultRecords.get(card))
    .filter(Boolean);
}

export { pushCard, clearResults };
