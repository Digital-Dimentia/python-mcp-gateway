"""The admin UI's static assets. No Python here, and deliberately so.

This file exists to make this directory a package, which is what lets setuptools ship the
assets beside it as package data and `importlib.resources` find them inside a wheel
exactly as it finds them in a checkout -- `src` is on `sys.path` in a checkout and
`site-packages` is after an install, so one lookup works in both.

It is a *top-level* package rather than `mcp_gateway.ui`, because the UI is not part of
the daemon: `src/mcp_gateway/` is the Python that serves MCP, this is the page a browser
and the Tauri shell load, and `src/desktop/` is that shell. The `mcp_gateway_` prefix is
the price of shipping at the top level -- a bare `ui` in `site-packages` is a name some
other distribution will eventually claim.

See `mcp_gateway/webui.py`, which reads these files, and `mcp_gateway/webui.md` for what
the UI is and why it is served from the same port as the two sockets.
"""
