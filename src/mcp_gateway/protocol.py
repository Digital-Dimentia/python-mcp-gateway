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
RESOURCES_SUBSCRIBE = "resources/subscribe"
RESOURCES_UNSUBSCRIBE = "resources/unsubscribe"
COMPLETION_COMPLETE = "completion/complete"
LOGGING_SET_LEVEL = "logging/setLevel"
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

#: Not an MCP method. A `url` backend's transport says this to itself after it has had to
#: run `initialize` a second time, because everything the gateway set on the old session --
#: resource subscriptions, `logging/setLevel` -- went with it, and only the gateway knows
#: what those were. It travels the notification channel a backend already has, so nothing
#: new has to be wired from a transport up to `Gateway`. The `x-` keeps it clear of any
#: method MCP may one day define; a backend that sent it itself would be asking for exactly
#: the same repair, so nothing checks where it came from.
SESSION_RENEWED = "notifications/x-mcp-gateway/session_renewed"

#: MCP Apps (SEP-1865), the extension that lets a server ship a UI for its tools.
#:
#: The identifier a **client** declares at `initialize`, under a top-level `extensions`
#: block, to say it can render one. Here for the same reason as everything else in this
#: module: `mcp_stdio.py` writes it facing a backend, `ui_apps.py` reads it facing a
#: catalogue, and two spellings would be a silent interop bug.
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"

#: The two content types a panel can be written in, and the two this gateway's own UI now
#: renders -- so the two it declares. Nothing is declared it cannot draw: a capability block
#: is a promise rather than a wish list, which is what `mcp_stdio.MCPClientCapabilities`
#: refuses to let anybody forget.
#:
#: The `profile` parameter is SEP-1865's own, which is what makes the two tiers addressable
#: the same way: a tool names a `ui://` resource, and the *resource* says which tier it is.
#: So a backend can offer both under one URI and let each host take the one it understands,
#: and upgrading a panel from one tier to the other never touches the tool definition.
#:
#: The declarative tier is JSON this gateway's UI turns into DOM, executing nothing (see
#: `ui_apps.md`). The HTML tier is a document the backend wrote, run in a sandboxed iframe
#: from an endpoint with its own policy (see `panels.md`). They are not two spellings of one
#: thing, and the difference is the whole of that second doc.
UI_APP_DECLARATIVE_MIME = "application/json;profile=mcp-app-declarative"
UI_APP_HTML_MIME = "text/html;profile=mcp-app"

#: The `profile` parameter alone, for the comparison `ui_apps.profile_of` is built to make.
#: Split out rather than parsed at each call site: a `mimeType` carries spacing and casing a
#: backend chose, so matching the whole string is a bug waiting for a server that writes
#: `text/html; profile=mcp-app`.
UI_APP_HTML_PROFILE = "mcp-app"
UI_APP_DECLARATIVE_PROFILE = "mcp-app-declarative"

#: MCP's log levels, least to most severe (RFC 5424's). The order is the whole point: a
#: client that asked for `warning` is asking for warning *and everything above it*, and a
#: gateway serving several clients has to ask its backends for the most verbose level any
#: of them wants and filter per client on the way up.
LOG_LEVELS: tuple[str, ...] = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)


def level_admits(threshold: str, level: str) -> bool:
    """Whether a record at `level` should reach a client that asked for `threshold`.

    An unknown level from a backend is admitted rather than dropped. A later revision may
    add one, and swallowing a message because we do not recognise its severity is the one
    failure mode a log relay must not have.
    """
    if level not in LOG_LEVELS:
        return True
    if threshold not in LOG_LEVELS:
        return True
    return LOG_LEVELS.index(level) >= LOG_LEVELS.index(threshold)


def most_verbose(levels: "list[str] | set[str]") -> str | None:
    """The level that satisfies every one of `levels`. `None` for an empty collection."""
    known = [level for level in levels if level in LOG_LEVELS]
    return min(known, key=LOG_LEVELS.index) if known else None


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
    Capability("resources", "subscribe", "router.subscribe_resource / notifications.Subscriptions"),
    Capability(
        "completions",
        None,
        "router.complete; empty values when the backend declares no completions",
    ),
    Capability(
        "logging",
        None,
        "gateway.set_log_level forwards down; backend notifications/message relayed up",
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

    `subscribe` used to be the one negative here, and is now a row like any other: the
    gateway keeps the subscription itself and forwards `notifications/resources/updated`
    with the URI rewritten into its own address space. It is unconditional for the same
    reason the rest of the block is -- a backend that cannot be subscribed to refuses that
    one `resources/subscribe`, which is a per-URI answer, not a claim about the gateway.
    """
    block: dict[str, dict[str, bool]] = {}
    for capability in CAPABILITY_MANIFEST:
        entry = block.setdefault(capability.key, {})
        if capability.sub_key:
            entry[capability.sub_key] = True
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
