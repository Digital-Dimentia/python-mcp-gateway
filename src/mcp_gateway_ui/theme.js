// The theme and the column widths: the state that has to be right before the first paint.
//
// This is a separate, render-blocking classic script rather than part of `app.js` for one
// reason: `app.js` is a module, modules are deferred, and a deferred script runs after the
// page has already been painted in whatever the OS said. Someone who chose light on a dark
// machine would watch the page flash dark on every reload. A CSP of `script-src 'self'`
// rules out the usual inline snippet, so it ships as a file.
//
// A dragged column split is the same class of state, with a louder failure: restored from a
// module, every reload shows one frame of the default layout and then jumps the width of
// the screen.

(function () {
  var STORAGE = 'mcp-gateway-ui.theme';

  // 'system' follows `prefers-color-scheme`, which is the behaviour the page had before a
  // toggle existed and stays the default. 'light' and 'dark' pin it.
  var MODES = ['system', 'light', 'dark'];

  function stored() {
    try {
      var value = localStorage.getItem(STORAGE);
      return MODES.indexOf(value) === -1 ? 'system' : value;
    } catch (_) {
      return 'system';   // private window, or site data blocked
    }
  }

  function apply(mode) {
    if (mode === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
  }

  // `app.js` reads these off the global; there is no module boundary to import across.
  window.__theme = {
    MODES: MODES,
    get: stored,
    set: function (mode) {
      apply(mode);
      try { localStorage.setItem(STORAGE, mode); } catch (_) { /* nothing to do */ }
    },
  };

  apply(stored());

  // ── The screen ─────────────────────────────────────────────────────────────
  //
  // Which layout the content panel is showing, as an attribute on <html> that `style.css`
  // reads. Here rather than in `app.js` for the reason everything else in this file is
  // here: restored from a deferred module, every reload of a page parked on another screen
  // would paint the columns first and then swap.
  //
  // This file deliberately does *not* know what the screens are. The register is the
  // `<option>` list in `index.html`, which is also the control; a name is stored if it
  // looks like a name, and `app.js` -- which can see the options, because by then the body
  // is parsed -- is what rejects one that is not on the list. The stylesheet makes an
  // unrecognised value show the columns rather than nothing, so the gap between the two is
  // a screen you did not ask for and never a blank page.

  var SCREEN = 'mcp-gateway-ui.screen';
  var NAME = /^[a-z][a-z0-9-]{0,31}$/;

  function storedScreen() {
    try {
      var value = localStorage.getItem(SCREEN);
      return NAME.test(value || '') ? value : null;
    } catch (_) {
      return null;       // private window, or site data blocked
    }
  }

  function applyScreen(name) {
    if (name) document.documentElement.setAttribute('data-screen', name);
    else document.documentElement.removeAttribute('data-screen');
  }

  window.__screen = {
    get: storedScreen,
    set: function (name) {
      if (!NAME.test(name || '')) return;
      applyScreen(name);
      try { localStorage.setItem(SCREEN, name); } catch (_) { /* nothing to do */ }
    },
  };

  applyScreen(storedScreen());

  // ── The column widths ──────────────────────────────────────────────────────
  //
  // Only ever an *override*: the defaults live in `style.css` and nowhere else, so resetting
  // is removing the override and there is no second copy of the split in here to drift from
  // the one the stylesheet ships. `app.js` owns the gesture and calls `save`.

  var COLUMNS = 'mcp-gateway-ui.columns';
  var PROPS = ['--col-1', '--col-2', '--col-3'];

  // Three positive, finite numbers and nothing else. This is the guard on a value that goes
  // straight into `grid-template-columns`; the `@property` registrations in `style.css` are
  // what catches whatever gets past it.
  function validColumns(values) {
    if (!Array.isArray(values) || values.length !== PROPS.length) return false;
    for (var i = 0; i < PROPS.length; i++) {
      var n = values[i];
      if (typeof n !== 'number' || !isFinite(n) || n <= 0) return false;
    }
    return true;
  }

  function applyColumns(values) {
    for (var i = 0; i < PROPS.length; i++) {
      document.documentElement.style.setProperty(PROPS[i], values[i] + 'fr');
    }
  }

  function resetColumns() {
    for (var i = 0; i < PROPS.length; i++) {
      document.documentElement.style.removeProperty(PROPS[i]);
    }
    try {
      localStorage.removeItem(COLUMNS);
    } catch (_) { /* private window, or site data blocked */ }
  }

  window.__columns = {
    valid: validColumns,
    apply: applyColumns,
    reset: resetColumns,
    save: function (values) {
      if (!validColumns(values)) return;
      applyColumns(values);
      try {
        localStorage.setItem(COLUMNS, JSON.stringify(values));
      } catch (_) { /* private window, or site data blocked */ }
    },
  };

  try {
    // An unreadable or half-written value is not a reason to show a broken page: fall
    // through to the stylesheet's default, exactly as an unknown theme falls back to
    // 'system'.
    var split = JSON.parse(localStorage.getItem(COLUMNS));
    if (validColumns(split)) applyColumns(split);
  } catch (_) { /* nothing stored, or no storage: the stylesheet's default stands */ }
})();
