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
    assert.deepEqual(ui.SCREENS, ['basics', 'about', 'connection']);
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

  it('offers only the screens this host can show', () => {
    //: Connection is the desktop shell's own subject -- the child process, the SSH tunnel --
    //: and in a browser its controls would be wired to nothing. So the option is removed
    //: rather than disabled: a disabled option reads as something you might one day be
    //: allowed to pick, and in a browser there is no such day.
    assert.deepEqual(ui.AVAILABLE, ['basics', 'about']);
    assert.equal(document.querySelector('#screen-select option[value="connection"]'), null);
    //: The register is still the full list, so the two checks above -- a section and a
    //: module for every screen -- keep covering the one this host is not showing.
    assert.ok(ui.SCREENS.includes('connection'));
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

  // The other way a stored name goes stale: the desktop app stored `connection`, and the
  // same profile is later opened in a browser. Unlike a slug from a later build, the
  // stylesheet *recognises* this one -- so leaving the attribute alone would show an empty
  // panel forever rather than falling through to Basics.
  it('answers a screen this host cannot show the same way', () => {
    ui.showScreen('connection');
    assert.equal(document.getElementById('screen-select').value, 'basics');
    assert.equal(html().getAttribute('data-screen'), 'basics');
    assert.equal(stored(), 'basics');
  });
});

describe('the About screen', () => {
  const about = () => document.getElementById('about');

  it('renders with nothing connected', () => {
    ui.showScreen('about');
    const headings = [...about().querySelectorAll('.about-card h2')].map((h) => h.textContent);
    assert.deepEqual(headings, ['The tour', 'Gateway']);
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

  it('puts each Gateway field in its column: connect, running, read', () => {
    //: The status fixture from the endpoints test above, which has a bind address.
    ui.SCREEN_MODULES.about.refresh(ui.state);
    const column = (n) => [...about().querySelectorAll(`.about-fields .about-col-${n} dt`)]
      .map((dt) => dt.textContent);
    assert.deepEqual(column(1), ['MCP', 'Admin']);
    assert.deepEqual(column(2), ['Name', 'Version', 'Uptime']);
    assert.deepEqual(column(3), ['Config', 'Env']);
    //: And MCP is first in the written order too, which is what the one-column fold shows.
    assert.equal(about().querySelector('.about-fields dt').textContent, 'MCP');
  });

  it('has no card per server: the header already has one per server', () => {
    //: Three backends, one running -- the fixture the tour's last slide is quoted against below.
    ui.state.backends = [
      { name: 'zoo', enabled: true, status: 'running', description: 'the schema zoo' },
      { name: 'broken', enabled: true, status: 'failed', error: 'No such file or directory' },
      { name: 'parked', enabled: false, status: 'stopped' },
    ];
    ui.SCREEN_MODULES.about.refresh(ui.state);
    const headings = [...about().querySelectorAll('.about-card h2')].map((h) => h.textContent);
    assert.ok(!headings.some((h) => h.startsWith('Servers')), headings.join(', '));
  });
});

// `admin.secrets.keys` on About. The acceptance line was two-sided and so is this: a chain
// renders a row per key naming its source, and a store built from the file alone -- no
// providers -- leaves the screen exactly as it was.
describe('where each secret came from', () => {
  const about = () => document.getElementById('about');
  const card = () => [...about().querySelectorAll('.about-card')]
    .find((c) => c.querySelector('h2')?.textContent.startsWith('Secrets'));

  it('is not shown when no provider is configured', () => {
    ui.state.secrets = { keys: ['API_TOKEN'], origins: {}, providers: [] };
    ui.SCREEN_MODULES.about.refresh(ui.state);
    assert.equal(card(), undefined);
    assert.doesNotMatch(about().textContent, /null/);
  });

  it('names the source of every key once a provider is', () => {
    ui.state.secrets = {
      keys: ['GITHUB_TOKEN', 'WS_ACCESS_KEY'],
      origins: { GITHUB_TOKEN: 'vault_provider:Provider', WS_ACCESS_KEY: 'gateway.env' },
      providers: ['vault_provider:Provider'],
    };
    ui.SCREEN_MODULES.about.refresh(ui.state);

    assert.ok(card(), 'there is a Secrets card');
    assert.equal(card().querySelector('h2').textContent, 'Secrets (2)');
    const pairs = [...card().querySelectorAll('dt')]
      .map((dt) => `${dt.textContent}=${dt.nextElementSibling.textContent}`);
    assert.deepEqual(pairs, [
      'GITHUB_TOKEN=vault_provider:Provider',
      'WS_ACCESS_KEY=gateway.env',
    ]);
    //: The file is the last link whether or not the payload lists it.
    assert.match(card().textContent, /vault_provider:Provider → gateway\.env/);
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
