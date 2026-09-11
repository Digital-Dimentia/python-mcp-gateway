"""An MCP gateway daemon: one WebSocket MCP server, many stdio backends, one credential store.

The version literal here is the single source of truth at runtime and is bound to the
installed distribution's metadata by `tests/test_version.py`, so the two cannot drift
without a test failing.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
