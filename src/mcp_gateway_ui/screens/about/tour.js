// The deck: one slide at a time, and the controls that move between them.
//
// This file is the mechanism and nothing else. It knows how to show slide n of m, how to
// get to n+1, and how to keep that choice while the screen around it is being repainted;
// it knows nothing about gateways, backends or credentials. What a slide *says* is in
// `slides.js`, and `screen.js` is what puts the two together. The split is the whole
// design: the content is the part that will be edited every release, and editing it should
// never mean reading a keyboard handler.
//
// ## Why a deck, and not a page of prose
//
// The About screen already reads the running gateway off the two admin payloads. What it
// could not say is what the thing *is* -- the value, the shape, the two files, what it is
// built on. That is in `README.md` and `GET_STARTED.md`, neither of which is in the window,
// and a desktop-app user may have never seen either: they double-clicked an icon. So the
// explanation goes where the person already is, in the form that survives being read for
// fifteen seconds and abandoned -- one idea per card, in an order, with an obvious way out.
//
// ## What holds the position
//
// `index` is here, in the closure, rather than on the rendered DOM or in `state`. About
// repaints whenever the payloads move, and a deck that jumped back to slide 1 because a
// backend changed state would be unusable in exactly the moment somebody is watching a
// backend change state. `screen.js` therefore builds the deck once and re-appends the same
// node, and `update` is how live figures on a slide are refreshed without rebuilding it.
//
// It is deliberately *not* in `localStorage`. The theme and the screen are remembered
// because they are preferences; where you were in an explanation is not one, and a tour
// that reopens on slide 5 is a tour that never gets read from the top.

/**
 * The 12-line DOM shim every module in this UI that builds DOM carries a copy of, and the
 * canonical statement of why they copy it: it is a DOM constructor with no decisions in it,
 * so there is nothing in it that can drift, and one more shared module would be one more
 * asset and one more GET on a page that has no bundler. What is *not* copied anywhere is
 * something that formats a value -- see `format.js`, which exists because an uptime that
 * read two ways in one window was a real bug.
 *
 * Exported here because `slides.js` and `screen.js` are this screen's own files and already
 * fetch this one, so for them the second half of that argument buys nothing.
 */
export const el = (tag, props = {}, children = []) => {
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

/**
 * Build a deck out of a list of slides.
 *
 * A slide is `{ id, title, tag, render(state) }` -- `render` returns the body, and is
 * called once per visit rather than once ever, so a slide may read the payloads. The
 * returned object is `{ node, update(state) }`: `node` goes in the page and stays there,
 * `update` is what a repaint calls.
 */
export function deck(slides) {
  let index = 0;
  let latest = null;

  const stage = el('div', { class: 'tour-stage', id: 'tour-stage', role: 'group' });
  //: `aria-live="polite"`, not `assertive`: moving a slide is a thing the reader just did,
  //: so it should be announced after whatever they are currently hearing, not over it.
  stage.setAttribute('aria-live', 'polite');

  const counter = el('span', { class: 'tour-counter' });
  const prev = el('button', {
    type: 'button', class: 'ghost tour-arrow', text: '‹', title: 'Previous (←)',
  });
  const next = el('button', {
    type: 'button', class: 'ghost tour-arrow', text: '›', title: 'Next (→)',
  });
  prev.setAttribute('aria-label', 'Previous slide');
  next.setAttribute('aria-label', 'Next slide');

  //: One dot per slide, and each is a button rather than a decoration. A deck of nine
  //: slides whose only navigation is "next" is a deck where slide 8 is unreachable to
  //: anyone who wanted to reread it.
  const dots = el('div', { class: 'tour-dots', role: 'tablist' });
  const dotNodes = slides.map((slide, i) => {
    const dot = el('button', {
      type: 'button', class: 'tour-dot', title: slide.title, role: 'tab',
    });
    dot.setAttribute('aria-label', slide.title);
    dot.addEventListener('click', () => go(i));
    dots.append(dot);
    return dot;
  });

  const nav = el('div', { class: 'tour-nav' }, [prev, dots, counter, next]);

  //: `tabindex` on the deck rather than a document-level key handler. The arrow keys belong
  //: to whatever the reader is actually in -- a filter box, the primitives list, a form --
  //: and a deck that stole them page-wide would break the column screen from a different
  //: screen's code. Focus the deck, and the arrows are the deck's.
  const node = el('section', {
    class: 'about-card tour', id: 'tour', tabindex: '0',
  }, [
    el('div', { class: 'tour-head' }, [
      el('h2', { text: 'The tour' }),
      el('p', { class: 'tour-sub', text: 'What this is, how it is put together, and how to configure it.' }),
    ]),
    stage,
    nav,
  ]);
  node.setAttribute('aria-roledescription', 'carousel');
  node.setAttribute('aria-label', 'A tour of the gateway');

  node.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft') go(index - 1);
    else if (event.key === 'ArrowRight') go(index + 1);
    else if (event.key === 'Home') go(0);
    else if (event.key === 'End') go(slides.length - 1);
    else return;
    event.preventDefault();
  });

  prev.addEventListener('click', () => go(index - 1));
  next.addEventListener('click', () => go(index + 1));

  /** Show slide `n`, clamped. Redraws the body from whatever state was last handed in. */
  function go(n) {
    index = Math.max(0, Math.min(slides.length - 1, n));
    draw();
  }

  function draw() {
    const slide = slides[index];
    stage.dataset.slide = slide.id;
    stage.replaceChildren(
      el('div', { class: 'tour-title' }, [
        el('h3', { text: slide.title }),
        slide.tag ? el('span', { class: 'tour-tag', text: slide.tag }) : null,
      ]),
      slide.render(latest || {}),
    );
    counter.textContent = `${index + 1} / ${slides.length}`;
    prev.disabled = index === 0;
    next.disabled = index === slides.length - 1;
    dotNodes.forEach((dot, i) => {
      dot.classList.toggle('on', i === index);
      dot.setAttribute('aria-selected', String(i === index));
    });
  }

  return {
    node,
    /** New payloads. Redraws the current slide only -- the position is not state. */
    update(state) {
      latest = state;
      draw();
    },
    //: For the test suite, and for nothing else: the deck's position is not a thing any
    //: other module is allowed to set.
    at: () => index,
  };
}
