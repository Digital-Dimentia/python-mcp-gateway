"""Which of a backend's tools the MCP port advertises.

One set of names per backend, in memory, shared by every connection. `tools/list` on `/mcp`
leaves out what is in it; nothing else in the daemon consults it.

See [`visibility.md`](visibility.md) for why it holds what is *hidden* rather than what is
allowed, why a name is never pruned against the live listing, and why hiding is context
economy rather than access control.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

#: The `clientInfo.name` the admin UI's own `/mcp` session sends, and the one client that
#: gets the unfiltered listing. Mirrored by `src/mcp_gateway_ui/rpc.js`, whose `initialize`
#: sends it -- the two literals are a pair, and the comment there names this constant.
BENCH_CLIENT = "mcp-gateway-ui"


class ToolVisibility:
    """The hidden sets, keyed by backend name.

    Names are the backend's **own** spelling -- `create_issue`, not `github__create_issue`.
    That is what the admin method takes and what the checkbox rows show, and splitting a
    public name to check one is free next to the fan-out a listing already is.
    """

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def is_hidden(self, server: str, local: str) -> bool:
        return local in self._hidden.get(server, ())

    def hidden(self, server: str) -> frozenset[str]:
        return frozenset(self._hidden.get(server, ()))

    def set_hidden(self, server: str, names: Iterable[str]) -> bool:
        """Replace one backend's hidden set. True if that changed anything.

        A whole-set replace rather than a per-name toggle: select-all and deselect-all are
        then one call each, the payload is idempotent, and two `/admin` connections cannot
        interleave into a half-applied state. The answer is what the caller guards the
        `list_changed` broadcast on, so re-sending an identical set is silent.
        """
        wanted = {name for name in names if isinstance(name, str) and name}
        if wanted == self._hidden.get(server, set()):
            return False
        if wanted:
            self._hidden[server] = wanted
        else:
            self._hidden.pop(server, None)
        return True

    def drop_backend(self, server: str) -> None:
        """Forget a backend's selection. For one a reload removed, and nothing else.

        Not for a restart, an Enable/Disable or an Edit: those are the same backend, and a
        selection that did not survive them would be a selection nobody could rely on.
        """
        self._hidden.pop(server, None)

    def snapshot(self) -> dict[str, list[str]]:
        """Every backend's hidden names, sorted. For the admin payloads."""
        return {server: sorted(names) for server, names in self._hidden.items()}

    @staticmethod
    def exempt(session: Any) -> bool:
        """Whether this connection gets the unfiltered listing.

        True for the admin UI's own `/mcp` session, which is a test bench: what it cannot
        see it cannot un-hide. **Not a trust boundary** -- a hidden tool is still callable by
        anyone, so a client that lies about its `clientInfo` gains a longer list and nothing
        else. See `visibility.md`.
        """
        return (getattr(session, "client_info", None) or {}).get("name") == BENCH_CLIENT
