// The screen selector, and what the About screen makes of the two admin payloads.
//
// Its own file because `boot()` is once per process -- see `harness.mjs`. What is asserted
// is the wiring and the arithmetic, never the appearance: jsdom loads no stylesheet and
// lays nothing out, so "the columns are hidden" is not a question that can be asked here.
// What *can* be asked is the thing the stylesheet is driven by -- the `data-screen`
// attribute on <html> -- and whether the stored name survives, is corrected, and is written
// only when someone chose it.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';

import { boot } from './harness.mjs';

let ui;
let document;
let window;

const html = () => document.documentElement;
const stored = () => window.localStorage.getItem('mcp-gateway-ui.screen');

before(async () => {
  ({ ui, document, window } = await boot());
});

after(() => {
  ui.state.admin.close();
  ui.state.mcp.close();
});

describe('the selector', () => {
  it('is in the header, after the title', () => {
    const select = document.getElementById('screen-select');
    assert.ok(select, 'there is a screen selector');
    assert.ok(document.querySelector('header.bar').contains(select), 'and it is in the header');
    const title = document.getElementById('brand-title');
    //: DOCUMENT_POSITION_FOLLOWING: the selector comes after the name, not before it.
    assert.equal(title.compareDocumentPosition(select) & 4, 4);
  });

  it('is the register of screens, and every option has a section and a module', () => {
    assert.deepEqual(ui.SCREENS, ['basics', 'about']);
    for (const name of ui.SCREENS) {
      assert.ok(
        document.querySelector(`section.screen[data-screen="${name}"]`),
        `${name} has a section in the content panel`,
      );
      //: The failure this catches is a screen wired into the markup and the stylesheet but
      //: never registered: an empty section, and no error anywhere.
      assert.equal(ui.SCREEN_MODULES[name]?.id, name, `${name} has a module`);
    }
  });

  it('opens on the first screen, and stores nothing until someone chooses', () => {
    assert.equal(document.getElementById('screen-select').value, 'basics');
    assert.equal(html().getAttribute('data-screen'), null);
    assert.equal(stored(), null);
  });
});

describe('choosing a screen', () => {
  it('writes the attribute the stylesheet reads, and remembers it', () => {
    ui.showScreen('about');
    assert.equal(html().getAttribute('data-screen'), 'about');
    assert.equal(stored(), 'about');
    assert.equal(document.getElementById('screen-select').value, 'about');
  });

  it('goes back', () => {
    ui.showScreen('basics');
    assert.equal(html().getAttribute('data-screen'), 'basics');
    assert.equal(stored(), 'basics');
  });

  // The failure this guards is a build that drops a screen while somebody's browser is
  // still parked on it. The stylesheet answers an unrecognised name with Basics; this
  // is the other half -- the page agreeing with it rather than sitting on a name it has
  // nothing to show for.
  it('falls back to the first screen when the name is not one of them, and corrects it', () => {
    ui.showScreen('a-screen-from-a-later-build');
    assert.equal(document.getElementById('screen-select').value, 'basics');
    assert.equal(html().getAttribute('data-screen'), 'basics');
    assert.equal(stored(), 'basics');
  });
});

describe('the About screen', () => {
  const about = () => document.getElementById('about');

  it('renders with nothing connected', () => {
    ui.showScreen('about');
    assert.match(about().textContent, /No servers configured/);
  });

  it('shows the endpoints and the files the gateway read', () => {
    ui.state.status = {
      uptime_seconds: 90,
      version: '9.9.9',
      bind: '127.0.0.1:8765',
      connections: [{ path: '/mcp' }, { path: '/mcp' }, { path: '/admin' }],
      config_path: '/etc/mcp/servers.yaml',
      env_path: '/etc/mcp/gateway.env',
    };
    ui.SCREEN_MODULES.about.refresh(ui.state);

    const text = about().textContent;
    assert.match(text, /127\.0\.0\.1:8765\/mcp/);
    assert.match(text, /127\.0\.0\.1:8765\/admin/);
    assert.match(text, /\/etc\/mcp\/servers\.yaml/);
    assert.match(text, /\/etc\/mcp\/gateway\.env/);
    assert.match(text, /1m/);                        // the uptime, formatted
  });

  it('names each server, its state, and why it is not running', () => {
    ui.state.backends = [
      {
        name: 'zoo', enabled: true, status: 'running', description: 'the schema zoo',
        command: 'python', args: ['examples/zoo_server.py'], error: null,
      },
      {
        name: 'broken', enabled: true, status: 'failed', description: '',
        command: 'nope', args: [], error: 'No such file or directory',
      },
      {
        name: 'parked', enabled: false, status: 'stopped', description: '',
        command: 'python', args: [], error: null,
      },
    ];
    ui.state.missing = { broken: ['API_TOKEN'] };
    ui.SCREEN_MODULES.about.refresh(ui.state);

    const rows = [...about().querySelectorAll('.about-server')];
    //: In the catalogue's order, which is the order the header's server row is in.
    assert.deepEqual(
      rows.map((r) => r.querySelector('.about-name').firstChild.textContent),
      ['zoo', 'broken', 'parked'],
    );
    //: The description hangs off the name rather than being a row of its own.
    assert.equal(rows[0].querySelector('.about-desc').textContent, 'the schema zoo');
    assert.equal(rows[1].querySelector('.about-desc'), null);

    // The indicator is the header's, so a server reads the same in both places.
    assert.ok(rows[0].querySelector('.dot.dot-running'));
    assert.ok(rows[1].querySelector('.dot.dot-failed'));

    // A disabled server says so rather than reporting the state of a process that was
    // never started.
    assert.equal(rows[2].querySelector('.about-state').textContent, 'disabled');

    assert.match(rows[0].textContent, /python examples\/zoo_server\.py/);
    assert.match(rows[1].textContent, /missing API_TOKEN/);
    assert.match(rows[1].textContent, /No such file or directory/);
  });
});

// The deck at the top of About. What is asserted is the mechanism -- which slide is
// showing, what moves it, and that a repaint does not move it -- plus the three figures a
// slide quotes off the payloads. What a slide *says* is prose in `slides.js` and is not
// something a test can hold still; the one content assertion here is the tag list, because
// the deck losing a section is a regression and reordering its wording is not.
describe('the tour', () => {
  const tour = () => document.getElementById('tour');
  const dots = () => [...tour().querySelectorAll('.tour-dot')];
  const counter = () => tour().querySelector('.tour-counter').textContent;
  const arrows = () => [...tour().querySelectorAll('.tour-arrow')];
  const title = () => tour().querySelector('.tour-title h3').textContent;

  const press = (key) => tour().dispatchEvent(
    new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }),
  );

  it('sits above the cards, and opens on the first slide', () => {
    ui.showScreen('about');
    assert.ok(tour(), 'the deck is on the About screen');
    //: DOCUMENT_POSITION_FOLLOWING: the explanation comes before the live cards.
    const gateway = document.querySelector('#about .about-card:not(.tour)');
    assert.equal(tour().compareDocumentPosition(gateway) & 4, 4);
    assert.equal(counter(), `1 / ${dots().length}`);
    assert.ok(dots()[0].classList.contains('on'));
  });

  it('covers every section the tour is for', () => {
    const tags = new Set();
    for (let i = 0; i < dots().length; i += 1) {
      dots()[i].click();
      tags.add(tour().querySelector('.tour-tag').textContent);
    }
    for (const section of ['What it is', 'The case', 'Architecture', 'Configuration', 'The admin UI', 'Technology']) {
      assert.ok(tags.has(section), `the deck still has a ${section} slide`);
    }
    dots()[0].click();
  });

  it('moves with the arrows, the dots and the keyboard, and stops at both ends', () => {
    const [back, forward] = arrows();
    assert.equal(back.disabled, true, 'there is nothing before the first slide');

    forward.click();
    assert.equal(counter(), `2 / ${dots().length}`);
    assert.equal(back.disabled, false);

    press('ArrowLeft');
    assert.equal(counter(), `1 / ${dots().length}`);
    //: Clamped rather than wrapped: a deck that jumps to the end when you press back on
    //: slide 1 has just thrown the reader out of the explanation they were following.
    press('ArrowLeft');
    assert.equal(counter(), `1 / ${dots().length}`);

    press('End');
    assert.equal(counter(), `${dots().length} / ${dots().length}`);
    assert.equal(arrows()[1].disabled, true, 'there is nothing after the last slide');

    dots()[2].click();
    assert.equal(counter(), `3 / ${dots().length}`);
    assert.ok(dots()[2].classList.contains('on'));
  });

  // The failure this pins: About repaints whenever a payload moves, and a deck rebuilt on
  // every repaint would throw the reader back to slide 1 the moment a backend changed
  // state -- which is exactly when somebody is watching the screen.
  it('stays where it was when the screen repaints', () => {
    const was = { counter: counter(), title: title() };
    ui.SCREEN_MODULES.about.refresh(ui.state);
    assert.equal(counter(), was.counter);
    assert.equal(title(), was.title);
  });

  it('quotes the running gateway on its last slide', () => {
    press('End');
    const text = tour().querySelector('.tour-stage').textContent;
    //: The fixtures above: three backends, one of them running, and two /mcp clients.
    assert.match(text, /1 running of 3 configured/);
    assert.match(text, /2 on \/mcp, 1 on \/admin/);
    assert.match(text, /127\.0\.0\.1:8765\/mcp/);
  });

  it('reads before either socket has answered', () => {
    //: The case a first-run user actually sees: no status, no backends, and every live
    //: figure with nothing behind it. Every slide, because the failure mode of a tour that
    //: quotes the payloads is one card in nine reading `undefined` on the first launch.
    ui.SCREEN_MODULES.about.refresh({ backends: [], config: { servers: {} }, missing: {} });
    for (let i = 0; i < dots().length; i += 1) {
      dots()[i].click();
      const text = tour().querySelector('.tour-stage').textContent;
      assert.ok(text.length > 40, `slide ${i + 1} has something on it`);
      assert.doesNotMatch(text, /undefined|NaN|\[object/, `slide ${i + 1} is readable`);
    }
    assert.match(tour().textContent, /not listening yet/);
  });
});
