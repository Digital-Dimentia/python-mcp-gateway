# `__init__.py`

The package's version literal and nothing else.

`__version__` is what `clientInfo` sends to every backend and what `serverInfo` returns
to every client, so it has to be readable without importing anything that touches a
socket or a subprocess. `tests/test_version.py` binds it to
`importlib.metadata.version("python-mcp-gateway")`, which is read from the installed
distribution rather than from `pyproject.toml`, so the test only passes against a real
install — which is the state the gate runs in.

Deliberately empty otherwise: a package `__init__` that re-exports the public API turns
every import of any module into an import of all of them, and this package has modules
that spawn processes at import-adjacent moments.
