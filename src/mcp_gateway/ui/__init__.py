"""The admin UI's static assets. No Python here, and deliberately so.

This file exists to make `ui/` a package, which is what lets setuptools ship the six
assets beside it as package data and `importlib.resources` find them inside a wheel. See
`webui.py`, which reads them, and `webui.md` for what the UI is and why it is served from
the same port as the two sockets.
"""
