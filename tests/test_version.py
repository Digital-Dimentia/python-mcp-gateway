"""The version literal and the installed distribution's metadata must agree.

Two places can hold a version in a setuptools project -- `pyproject.toml` and the package
-- and nothing makes them agree on its own. This reads the *installed* metadata rather
than parsing `pyproject.toml`, so it only passes against a real install, which is the
state `make test` runs in.
"""

from __future__ import annotations

from importlib import metadata

import mcp_gateway

DIST_NAME = "python-mcp-gateway"


def test_package_version_matches_distribution_metadata() -> None:
    assert mcp_gateway.__version__ == metadata.version(DIST_NAME)
