"""MCP method names, protocol versions, and the two capability blocks.

The gateway speaks MCP in both directions, so every constant here is used twice: once
facing a backend (`mcp_stdio.py`) and once facing a client (`transport_ws.py`). Two copies
would be two things to keep in step, and a mismatch between what we accept from a backend
and what we accept from a client is a silent interop bug rather than a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The revision proposed at `initialize`, in both directions.
#:
#: `2025-06-18` rather than `2024-11-05` because `elicitation` is a client capability the
#: gateway forwards, and it does not exist before that revision. Nothing in the framing,
#: the handshake or the shutdown sequence differs between the two; the fields the newer
#: revision adds (`structuredContent`, resource links) are passed through untouched, which
#: is what a proxy wants anyway.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: The revisions we can actually speak. A peer's answer is authoritative and may name a
#: revision we never proposed; anything outside this set means we hang up rather than
#: guess. A newer revision is a capability claim, not a string swap.
#:
#: `2024-11-05` stays *accepted* while no longer being *proposed*: a backend pinned to it
#: must counter with it, and hanging up on that counter would drop every server that has
#: not moved yet. Everything the gateway calls exists in both; only `elicitation/create`
#: is newer, and a backend that countered with `2024-11-05` will never send one -- safe
#: rather than lossy, because it is the backend that would have asked.
SUPPORTED_MCP_PROTOCOL_VERSIONS: frozenset[str] = frozenset({MCP_PROTOCOL_VERSION, "2024-11-05"})

# --- method names ---------------------------------------------------------------------
# Spelled once so a typo is a NameError at import rather than a `-32601` at runtime.
INITIALIZE = "initialize"
INITIALIZED = "notifications/initialized"
PING = "ping"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
PROMPTS_LIST = "prompts/list"
PROMPTS_GET = "prompts/get"
RESOURCES_LIST = "resources/list"
RESOURCES_TEMPLATES_LIST = "resources/templates/list"
RESOURCES_READ = "resources/read"
COMPLETION_COMPLETE = "completion/complete"
CANCELLED = "notifications/cancelled"
PROGRESS = "notifications/progress"
MESSAGE = "notifications/message"
TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
PROMPTS_LIST_CHANGED = "notifications/prompts/list_changed"
RESOURCES_LIST_CHANGED = "notifications/resources/list_changed"
RESOURCES_UPDATED = "notifications/resources/updated"
ROOTS_LIST = "roots/list"
SAMPLING_CREATE_MESSAGE = "sampling/createMessage"
ELICITATION_CREATE = "elicitation/create"

#: Server-to-client requests the gateway relays upward when a client declared the matching
#: capability. Everything else a backend asks gets `-32601`.
RELAYED_SERVER_REQUESTS = frozenset({ROOTS_LIST, SAMPLING_CREATE_MESSAGE, ELICITATION_CREATE})


@dataclass(frozen=True)
class Capability:
    """One advertised capability and the reason it can be advertised.

    The manifest pattern, lifted from the template: a capability is only listed here if
    something answers it, and `tests/test_capabilities.py` refuses any key in
    `advertised_capabilities()` that has no row. Declaring something nothing answers is
    worse than declaring nothing -- it entitles a peer to a request that will fail.
    """

    key: str
    sub_key: str | None
    answered_by: str


#: Why each advertised key is honest. See `protocol.md` for the one subtlety: these are
#: advertised **unconditionally**, not as the live union of what the backends offer.
CAPABILITY_MANIFEST: tuple[Capability, ...] = (
    Capability("tools", "listChanged", "catalogue.tools / router.call_tool; admin tools always exist"),
    Capability("prompts", "listChanged", "catalogue.prompts / router.get_prompt; empty list when none"),
    Capability("resources", "listChanged", "catalogue.resources / router.read_resource"),
    Capability(
        "completions",
        None,
        "router.complete; empty values when the backend declares no completions",
    ),
)


def advertised_capabilities() -> dict[str, dict[str, bool]]:
    """What the gateway promises its clients at `initialize`.

    **Unconditional, not the live union of the backends' capabilities.** That looks like a
    violation of "never declare what nothing answers", so the reasoning belongs in code
    rather than only in the doc:

    MCP capabilities are fixed at `initialize` and cannot change afterwards. A backend that
    starts failed and comes up later via `gateway__reload_config` would need `prompts` to
    *appear* on an already-negotiated connection -- structurally impossible. Advertising
    the live union is therefore broken by construction, and advertising the union of the
    *configured* set would make the block depend on subprocess startup races: two runs of
    one config could advertise differently.

    The rule is honoured in substance because the gateway can **always** answer. A
    `prompts/list` with no prompt-capable backend returns `{"prompts": []}`, which is
    exactly what a real server with no prompts returns and a case every client already
    handles. Nobody is ever stranded on a `-32601`.

    `completions` is the one key where "we can always answer" costs something, and the cost
    is worth naming. A backend that never declared `completions` cannot be asked, so the
    gateway answers `{"values": []}` on its behalf -- which is exactly what a
    completions-capable server returns for an argument it has no suggestions for, and
    therefore conflates two facts a client might have liked to tell apart. It is not a
    distinction the spec exposes anywhere else either: a real server's empty list says
    nothing about whether it *could* have suggested something. So nothing is lost that a
    client could have used, and the alternative -- a `-32601` for a method we advertise --
    would be a lie about our own method table.

    `subscribe` is `False` and is the one thing here that is a live claim: nothing
    implements `resources/subscribe` yet, so saying otherwise would strand a caller.
    """
    block: dict[str, dict[str, bool]] = {}
    for capability in CAPABILITY_MANIFEST:
        entry = block.setdefault(capability.key, {})
        if capability.sub_key:
            entry[capability.sub_key] = True
    block["resources"]["subscribe"] = False
    return block


def negotiate_version(proposed: object) -> tuple[str, bool]:
    """Answer a peer's proposed protocol version.

    Returns `(version, agreed)`. A supported proposal is echoed back -- the spec's rule,
    and the only answer that lets a peer pinned to an older revision proceed. Anything else
    is countered with our own preferred version, which the peer may then refuse.

    A *missing* `protocolVersion` counts as the older revision rather than as an error:
    `2024-11-05` servers in the wild omit it, and hanging up on them would drop working
    backends over a field their revision did not require.
    """
    if proposed is None:
        return "2024-11-05", True
    if isinstance(proposed, str) and proposed in SUPPORTED_MCP_PROTOCOL_VERSIONS:
        return proposed, True
    return MCP_PROTOCOL_VERSION, False
