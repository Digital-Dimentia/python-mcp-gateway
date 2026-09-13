// The Clipboard: hand this bench session to an agent.
//
// A person exercises a backend here by hand, and what they end up with is evidence —
// these calls, these arguments, these answers, these values. The button in the Results
// column turns that into one text document, in a modal, editable before it is copied.
//
// **The document is not written here.** `clipboard.py` renders it, and this module posts a
// *snapshot* — the two right-hand columns as data — and asks for the rendering back. That
// is deliberate and is the whole design: `gateway__clipboard` on `/mcp` serves the same
// text to the model, and two renderers of one document drift. The person who edits the
// textarea and the model that calls the tool must not be reading different sessions.
//
// So the page publishes on every change rather than only when the modal opens. A snapshot
// that were only taken on a button press would make the tool report whatever the bench
// looked like the last time somebody happened to press it — which is the kind of staleness
// nothing on screen would explain. See `src/mcp_gateway/clipboard.md`.

const $ = (id) => document.getElementById(id);

//: How long a burst of column changes is allowed to settle before one snapshot goes up.
//: A fan-out lands a card per call and `renderVariables` runs on every pick, so the
//: unthrottled version would put a dozen posts on the socket for one click.
const PUBLISH_DEBOUNCE_MS = 400;

//: How long "Copied" stays on the button before it says what it does again.
const FLASH_MS = 1200;

let gather = null;
let send = null;
let timer = null;

//: Set while the modal is open, so a result that lands *behind* it refreshes the text
//: rather than leaving the person copying a document that is already out of date.
let open = false;

//: True once the person has typed into the textarea. A background refresh then stops
//: touching it: the edit is the point of the editor, and an arriving card is not a reason
//: to throw away what somebody wrote.
let edited = false;

/**
 * Wire the button, the modal and the publisher up.
 *
 * `gather()` returns the snapshot — app.js owns the columns, so it owns that. `send(method,
 * params)` is the `/admin` socket. Both are passed in rather than imported, which is what
 * keeps this module testable against a socket that only records.
 */
export function installClipboard({ gather: gatherFn, send: sendFn }) {
  gather = gatherFn;
  send = sendFn;

  $('btn-clipboard').addEventListener('click', openClipboard);
  $('btn-clip-copy').addEventListener('click', copyDocument);
  $('btn-clip-regen').addEventListener('click', () => { edited = false; refresh(); });
  $('clip-text').addEventListener('input', () => { edited = true; note('Edited — the copy button takes what is in the box.'); });
  $('clipboard-dialog').addEventListener('close', () => { open = false; });
}

/**
 * Publish the columns, soon.
 *
 * Fire-and-forget by design: every caller is an event handler with nobody to return to,
 * and a failed publish is not a thing to interrupt a person's work over — the modal asks
 * again when it opens, and that is where a failure is worth reporting.
 */
export function publishClipboard() {
  if (!gather || !send) return;
  clearTimeout(timer);
  timer = setTimeout(() => { publishNow().catch(() => {}); }, PUBLISH_DEBOUNCE_MS);
}

/** The publish itself, awaited by the modal and by tests. Returns the rendered document. */
export async function publishNow() {
  clearTimeout(timer);
  const result = await send('admin.clipboard.put', { snapshot: gather() });
  return result?.document ?? '';
}

async function openClipboard() {
  const dialog = $('clipboard-dialog');
  open = true;
  edited = false;
  $('clip-text').value = '';
  note('Generating…');
  if (!dialog.open) dialog.showModal();
  await refresh();
}

/** Re-publish and re-render, unless the person has edited the box. */
async function refresh() {
  if (!open || edited) return;
  const box = $('clip-text');
  try {
    const text = await publishNow();
    // Checked again on this side of the await, not only on the way in. A round trip to the
    // daemon is long enough to type in, and the version that only guarded at the top would
    // let a refresh started a moment before an edit land on top of it -- which is the one
    // thing an editor must never do. `tests/ui/clipboard.test.mjs` found it.
    if (!open || edited) return;
    box.value = text;
    // Said in the modal rather than only in the preamble: the person is about to paste
    // this somewhere, and how much of it there is decides whether they trim it first.
    const lines = box.value ? box.value.split('\n').length : 0;
    note(`${lines} lines, ${box.value.length.toLocaleString()} characters. Edit freely — `
      + 'copying takes what is in the box.');
  } catch (err) {
    note(`Could not generate the document: ${err.message || err}`);
  }
}

/** A card arrived, or a pick changed, while the modal was up. */
export function clipboardChanged() {
  publishClipboard();
  if (open && !edited) refresh().catch(() => {});
}

async function copyDocument() {
  const button = $('btn-clip-copy');
  const text = $('clip-text').value;
  const flash = (word) => {
    button.textContent = word;
    setTimeout(() => { button.textContent = 'Copy'; }, FLASH_MS);
  };
  try {
    await navigator.clipboard.writeText(text);
    flash('Copied');
  } catch {
    // A denied clipboard permission is not a dead end here: the text is already selected
    // and visible, and ⌘C is one keystroke. Saying so beats a bare failure.
    $('clip-text').select();
    flash('Copy blocked');
    note('The browser refused clipboard access. The document is selected — press ⌘C / Ctrl-C.');
  }
}

function note(text) {
  $('clip-note').textContent = text;
}
