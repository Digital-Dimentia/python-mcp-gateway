// The Basics screen: the three columns, and where their edges are.
//
// The work surface, and the screen the page opens on. What is *in* the three columns lives
// in a module each -- `primitives.js`, `results.js`, `variables.js`, with `detail.js` under
// the first -- because each is a piece of machinery in its own right. What is left here is
// the screen itself: the layout those columns sit in, the edges you drag between them, and
// the `show` hook that the screen contract exists for.
//
// ## Why `show` exists, and why this is the only screen that needs it
//
// A column cannot be measured while its screen is `display: none`, and the separators
// announce their position as a percentage of two measured widths. So coming back to this
// screen is not a repaint, it is a *measurement* -- which is exactly the thing a renderer
// called at any time cannot do. About has no such hook and needs none; see the contract at
// the head of `screens/about/screen.js`.
//
// Handed nothing, like `results.js`. The split is `theme.js`'s to restore before the first
// paint and this file's to change afterwards, and neither of them needs the gateway.
//
// ## The column widths
//
// `theme.js` owns the storage, the validation and the defaults, because a split restored
// from a deferred module lands after the first paint. This owns the gesture. The two
// gutters are the edges between the columns, and dragging one moves only the pair it
// divides -- their sum is held constant, so the third column does not shuffle sideways
// while you are still aiming at the second.
//
// Everything is measured in pixels and written back as `fr`. An `fr` value is a pure ratio,
// so a set of measured widths *is* a valid set of `fr` numbers: writing the measurements
// back reproduces the layout exactly, and keeps it proportional through a later window
// resize with no listener of our own. It is also the only sound basis for the arithmetic.
// Once a column is sitting on its floor the grid takes that track out of the flex
// distribution and shares the rest among the others, so the rendered widths stop following
// the stored ratio -- and a delta measured against the stored numbers would send the
// divider somewhere other than where the cursor is on the very first move.

const columnsEl = document.querySelector('.columns');
const colEls = [...columnsEl.querySelectorAll('.col')];
const gutterEls = [...columnsEl.querySelectorAll('.gutter')];

let colDrag = null;

/** The floor a drag clamps against, read off the stylesheet rather than copied from it. */
function columnFloor() {
  const raw = getComputedStyle(columnsEl).getPropertyValue('--col-min').trim();
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  if (raw.endsWith('rem')) return parseFloat(raw) * root;
  if (raw.endsWith('px')) return parseFloat(raw);
  return 16 * root;
}

const measureColumns = () => colEls.map((col) => col.getBoundingClientRect().width);

/** Widths as shares of 100, to a tenth: small, legible numbers that mean the same thing. */
function columnShares(widths) {
  const total = widths[0] + widths[1] + widths[2];
  return widths.map((w) => Math.round((w / total) * 1000) / 10);
}

/** A separator that moves is a widget, and a widget with no value announces nothing. */
function showColumnValues(widths) {
  //: Nothing to announce, and the arithmetic below would announce `NaN`: a column measures
  //: zero while another screen is showing, and on a page that opens on one this runs before
  //: anyone has looked at the columns. The value that is already in the markup stands until
  //: the columns are on the glass and can be measured -- `showScreen` re-asks then.
  if (!widths.every((w) => w > 0)) return;
  gutterEls.forEach((gutter, i) => {
    const share = (widths[i] / (widths[i] + widths[i + 1])) * 100;
    gutter.setAttribute('aria-valuenow', String(Math.round(share)));
  });
}

/**
 * The move itself. `i` names the pair, `delta` is in pixels, and it is clamped *before* it
 * is applied rather than after: clamping the result would let an overshoot accumulate out
 * of sight, and the divider would then sit still for the width of that overshoot on the way
 * back instead of picking the cursor up where it left it.
 */
function moveColumnEdge(widths, i, delta, floor) {
  const low = floor - widths[i];
  const high = widths[i + 1] - floor;
  //: Too narrow for both floors at once -- only reachable in the band between their total
  //: and the breakpoint where the columns stack. There is no honest move, so make none.
  if (low > high) return null;
  const step = Math.min(Math.max(delta, low), high);
  const next = widths.slice();
  next[i] += step;
  next[i + 1] -= step;
  return next;
}

function applyColumnDrag() {
  if (!colDrag) return;
  const next = moveColumnEdge(
    colDrag.widths, colDrag.index, colDrag.x - colDrag.startX, colDrag.floor,
  );
  if (!next) return;
  colDrag.shares = columnShares(next);
  window.__columns.apply(colDrag.shares);
  showColumnValues(next);
}

function endColumnDrag() {
  if (!colDrag) return;
  if (colDrag.frame) cancelAnimationFrame(colDrag.frame);
  colDrag.frame = 0;
  //: A press that never moved is a click, not a resize. Landing it anyway would store the
  //: split the page happens to be showing, which looks like nothing at all and quietly
  //: turns the stylesheet's default into a pinned layout that only a reset undoes.
  if (colDrag.x !== colDrag.startX) applyColumnDrag();   // the last position, not the last frame
  //: Written once, at the end. Persisting per frame would be a hundred serialisations of a
  //: number nobody has settled on yet.
  if (colDrag.shares) window.__columns.save(colDrag.shares);
  colDrag.gutter.classList.remove('on');
  document.documentElement.classList.remove('col-resizing');
  colDrag = null;
}

function resetColumns() {
  window.__columns.reset();
  showColumnValues(measureColumns());
}

for (const gutter of gutterEls) {
  gutter.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    //: Cancelling a `pointerdown` suppresses the compatibility mouse events in some
    //: engines, and `dblclick` -- the reset gesture -- with them. So selection is suppressed
    //: by a class instead, and the second click of a double is caught here as well.
    if (event.detail === 2) { resetColumns(); return; }

    const widths = measureColumns();
    //: Zero widths mean the stacked layout, or a page not laid out yet. Neither has an edge.
    if (!widths.every((w) => w > 0)) return;

    colDrag = {
      gutter,
      index: Number(gutter.dataset.gutter),
      startX: event.clientX,
      x: event.clientX,
      widths,                 // measured once: re-measuring mid-drag reads back what this
      floor: columnFloor(),   // same drag just wrote, and creeps by a rounding error a frame
      frame: 0,
      shares: null,
    };
    //: Pointer capture rather than window listeners: the pointer leaves the gutter on the
    //: first pixel and may leave the window entirely, and capture is what guarantees the
    //: `pointerup` that ends the gesture and the `pointercancel` that abandons it.
    if (gutter.setPointerCapture) gutter.setPointerCapture(event.pointerId);
    gutter.classList.add('on');
    document.documentElement.classList.add('col-resizing');
  });

  gutter.addEventListener('pointermove', (event) => {
    if (!colDrag || colDrag.gutter !== gutter) return;
    colDrag.x = event.clientX;
    //: One write per frame. A pointer reports faster than the screen refreshes, and every
    //: write here relays out three columns of results.
    if (colDrag.frame) return;
    colDrag.frame = requestAnimationFrame(() => {
      if (!colDrag) return;
      colDrag.frame = 0;
      applyColumnDrag();
    });
  });

  gutter.addEventListener('pointerup', endColumnDrag);
  gutter.addEventListener('pointercancel', endColumnDrag);

  // Back to the stylesheet's split, and the stored one forgotten: a reset that left the old
  // numbers in storage would bring them back on the next reload.
  gutter.addEventListener('dblclick', resetColumns);

  // The same gesture for someone who tabbed here. Measured fresh each time, because a
  // keypress is a whole gesture rather than one frame of one.
  gutter.addEventListener('keydown', (event) => {
    if (event.key === 'Home') {
      event.preventDefault();
      resetColumns();
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();                      // otherwise the arrows scroll the column
    const step = (event.shiftKey ? 64 : 16) * (event.key === 'ArrowLeft' ? -1 : 1);
    const widths = measureColumns();
    if (!widths.every((w) => w > 0)) return;
    const next = moveColumnEdge(widths, Number(gutter.dataset.gutter), step, columnFloor());
    if (!next) return;
    window.__columns.save(columnShares(next));
    showColumnValues(next);
  });
}

//: A window that changes size under a live drag invalidates the widths that drag was
//: measured from. Ending it is both the cheap fix and what the gesture looks like anyway.
window.addEventListener('resize', endColumnDrag);

showColumnValues(measureColumns());

export default {
  id: 'basics',

  //: No `refresh`. What this screen shows is three columns that keep themselves current off
  //: the sockets -- a listing arrives, a vocabulary is read, a call comes back -- so there is
  //: no moment at which the screen as a whole is redrawn from the state. `show` is the whole
  //: of its side of the contract.
  show() {
    showColumnValues(measureColumns());
  },
};
