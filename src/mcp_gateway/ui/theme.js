// The theme, applied before the first paint.
//
// This is a separate, render-blocking classic script rather than part of `app.js` for one
// reason: `app.js` is a module, modules are deferred, and a deferred script runs after the
// page has already been painted in whatever the OS said. Someone who chose light on a dark
// machine would watch the page flash dark on every reload. A CSP of `script-src 'self'`
// rules out the usual inline snippet, so it ships as a file.

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
})();
