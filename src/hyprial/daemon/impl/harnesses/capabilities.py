"""Harness capability declarations: the single source of truth.

One honest entry per (harness, mode) pair describing what the runtime can
actually do tonight.  Consumers query this table instead of scattering
``if harness == "claude"`` predicates; unlocking a capability in a later
phase means changing this table and its tests in the same PR, so capability
changes are explicit and reviewable.

Terminology: a *harness* is the agent runtime (claude=cc, pi, codex, jev); a
*provider* is a model vendor and never appears here.
"""

from __future__ import annotations

from hyprial.kernel import Capability, SupportLevel  # MP-3 import-back (symbols extracted to kernel)

from dataclasses import dataclass
from typing import Literal
from collections.abc import Iterable
from hyprial.kernel import DECLARED_HARNESSES as DECLARED_HARNESSES, HarnessLaunchSpec


PLUGIN_KINDS = frozenset({"mcp", "skill", "pi-extension", "claude-plugin"})

WHOLE_SESSION_SOURCES = frozenset({"claude-channel", "pi-extension"})
"""Installed-producer session sources whose observations cover the whole turn.

Only the Claude channel and the Pi extension install real producers today, so
only those sources make a cell's contact scope ``whole-session``; every other
source counts carrier deliveries only.
"""


@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    level: SupportLevel
    mechanism: str | None = None  # channel | agent_sdk | rpc | app_server
    note: str | None = None  # required for DEGRADED: state what is lost
    plugin_kinds: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.mechanism is not None and self.mechanism not in _KNOWN_MECHANISMS:
            raise ValueError(f"unknown mechanism {self.mechanism!r}")
        if self.level is SupportLevel.DEGRADED and not self.note:
            raise ValueError("degraded support must note what is lost")
        if not self.plugin_kinds <= PLUGIN_KINDS:
            raise ValueError(f"unknown plugin kinds {self.plugin_kinds - PLUGIN_KINDS}")


@dataclass(frozen=True, slots=True)
class HarnessConcurrency:
    """The bounded admission contract for a managed harness kind."""

    mode: Literal["sequential", "pool"]
    concurrency: int

    def __post_init__(self) -> None:
        if self.mode not in {"sequential", "pool"}:
            raise ValueError(f"unknown concurrency mode {self.mode!r}")
        if self.concurrency < 1:
            raise ValueError("harness concurrency must be positive")
        if self.mode == "sequential" and self.concurrency != 1:
            raise ValueError("sequential harnesses must have concurrency=1")


_KNOWN_MECHANISMS = frozenset(
    {
        "channel",
        "agent_sdk",
        "rpc",
        "app_server",
        "extension",
        "python_worker",
        "acp",
    }
)


def _native(
    mechanism: str | None = None, *, plugin_kinds: frozenset[str] = frozenset()
) -> CapabilitySupport:
    return CapabilitySupport(
        SupportLevel.NATIVE, mechanism, plugin_kinds=plugin_kinds
    )


def _degraded(
    mechanism: str | None,
    note: str,
    *,
    plugin_kinds: frozenset[str] = frozenset(),
) -> CapabilitySupport:
    return CapabilitySupport(
        SupportLevel.DEGRADED, mechanism, note, plugin_kinds
    )


def _unsupported(note: str | None = None) -> CapabilitySupport:
    return CapabilitySupport(SupportLevel.UNSUPPORTED, None, note)


# Keys present per row only where the mode applies ("-" in the design table
# means the key is omitted).  Values are tonight's honest baseline.
_DECLARATIONS: dict[tuple[str, bool], dict[Capability, CapabilitySupport]] = {
    ("claude", False): {
        Capability.INTERACTIVE_ATTACH: _native("channel"),
        Capability.WAKE_PUSH: _native("channel"),
        Capability.PROACTIVE_SEND: _native("channel"),  # harness-* MCP tools
        Capability.SESSION_LIFECYCLE: _native(),
        Capability.TOOL_INJECTION: _native("channel"),
        Capability.PLUGIN_INJECTION: _native(
            "channel",
            plugin_kinds=frozenset({"mcp", "skill", "claude-plugin"}),
        ),
        Capability.TURN_CONTROL: _native(),  # SDK interrupt on the managed path
        Capability.MODEL_SELECT: _native("channel"),
        Capability.PERMISSION_SET: _unsupported(
            "interactive claude policy is operator-owned; the authority apply "
            "loop manages daemon-launched headless workers"
        ),
    },
    ("claude", True): {
        Capability.HEADLESS_EXEC: _native("agent_sdk"),
        Capability.WAKE_PUSH: _native("agent_sdk"),  # daemon direct enqueue
        Capability.PROACTIVE_SEND: _native("agent_sdk"),  # WorkerChannel
        Capability.SESSION_LIFECYCLE: _native(),
        Capability.TOOL_INJECTION: _native("agent_sdk"),
        Capability.TURN_CONTROL: _native("agent_sdk"),
        Capability.EVENT_STREAM: _native("agent_sdk"),
        Capability.MODEL_SELECT: _native("agent_sdk"),
        Capability.PERMISSION_SET: _native("agent_sdk"),
    },
    ("pi", True): {
        Capability.HEADLESS_EXEC: _native("rpc"),
        Capability.WAKE_PUSH: _native("rpc"),  # daemon direct enqueue
        Capability.PROACTIVE_SEND: _native("rpc"),  # pi_harness_bridge.ts harness_send
        Capability.SESSION_LIFECYCLE: _native(),
        Capability.TOOL_INJECTION: _native("rpc"),  # -e pi_harness_bridge.ts
        Capability.TURN_CONTROL: _native("rpc"),  # abort
        Capability.EVENT_STREAM: _native("rpc"),  # agent_settled
        Capability.MODEL_SELECT: _native("rpc"),  # --provider/--model, set_model
        Capability.PERMISSION_SET: _unsupported(
            "pi has no launch-time allow/deny tool policy wired yet; adapter "
            "seam reserved"
        ),
    },
    ("pi", False): {
        Capability.INTERACTIVE_ATTACH: _native(
            "extension"
        ),  # pi_harness_attach.ts registers the TUI session
        Capability.WAKE_PUSH: _native(
            "extension"
        ),  # carrier polls and injects sendUserMessage(followUp)
        Capability.PROACTIVE_SEND: _native(
            "extension"
        ),  # harness_send via the bundled bridge toolset
        Capability.SESSION_LIFECYCLE: _native(
            "extension"
        ),  # the launcher owns --session-id (sanitized at the boundary)
        Capability.TOOL_INJECTION: _native(
            "extension"
        ),  # -e pi_harness_attach.ts carries the harness_* toolset
        Capability.PLUGIN_INJECTION: _native(
            "extension",
            plugin_kinds=frozenset({"skill", "pi-extension"}),
        ),  # --skill / --extension are session-scoped
        Capability.MODEL_SELECT: _native("extension"),
        Capability.PERMISSION_SET: _unsupported(
            "interactive pi policy is operator-owned"
        ),
    },
    ("codex", True): {
        Capability.HEADLESS_EXEC: _native("app_server"),
        Capability.WAKE_PUSH: _native("app_server"),
        Capability.PROACTIVE_SEND: _native("app_server"),
        Capability.SESSION_LIFECYCLE: _native(
            "app_server",
        ),  # the thread id is persisted to desired state and resumed via thread/resume on daemon restart
        Capability.TOOL_INJECTION: _native("app_server"),
        Capability.TURN_CONTROL: _native("app_server"),
        Capability.EVENT_STREAM: _native("app_server"),
        Capability.MODEL_SELECT: _native("app_server"),
        Capability.PERMISSION_SET: _unsupported(
            "codex exposes approval and sandbox policy, not an allow/deny tool "
            "list; adapter seam reserved"
        ),
    },
    ("codex", False): {
        Capability.INTERACTIVE_ATTACH: _native("app_server"),
        Capability.WAKE_PUSH: _native("app_server"),
        # the carrier delivers summaries; the model replies through the thread's harness-bridge MCP
        # Native only while the session runs with on-request approvals: under
        # `-a never` codex refuses every MCP tool call. The interactive launch
        # pins on-request and rejects `-a never`/bypass (ruling 77); the TUI user approves.
        Capability.PROACTIVE_SEND: _native("app_server"),
        Capability.SESSION_LIFECYCLE: _degraded(
            "app_server",
            "exact thread resume is supported; daemon-restart recovery of the "
            "interactive app-server/TUI is not yet supported",
        ),
        # same on-request approval condition as PROACTIVE_SEND above
        Capability.TOOL_INJECTION: _native("app_server"),
        Capability.PLUGIN_INJECTION: _degraded(
            "app_server",
            "session-scoped MCP servers are supported; skills and extensions are not",
            plugin_kinds=frozenset({"mcp"}),
        ),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.MODEL_SELECT: _native("app_server"),
        Capability.PERMISSION_SET: _unsupported(
            "interactive codex policy is operator-owned"
        ),
    },
    ("jev", True): {
        Capability.HEADLESS_EXEC: _native("python_worker"),
        Capability.WAKE_PUSH: _native("python_worker"),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported("stateless calls"),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _native("python_worker"),
        Capability.MODEL_SELECT: _native("python_worker"),
        Capability.PERMISSION_SET: _unsupported("jev has no tool surface"),
    },
    ("jev", False): {
        Capability.INTERACTIVE_ATTACH: _unsupported("jev is a headless stateless caller"),
        Capability.WAKE_PUSH: _unsupported(),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported("no persistent session"),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _unsupported(),
        Capability.MODEL_SELECT: _unsupported(),
        Capability.PERMISSION_SET: _unsupported("jev has no tool surface"),
    },
    ("agy", True): {
        Capability.HEADLESS_EXEC: _native("acp"),
        Capability.WAKE_PUSH: _native("acp"),
        Capability.PROACTIVE_SEND: _native("acp"),
        Capability.SESSION_LIFECYCLE: _native("acp"),
        Capability.TOOL_INJECTION: _native("acp"),
        Capability.TURN_CONTROL: _native("acp"),
        Capability.EVENT_STREAM: _native("acp"),
        Capability.MODEL_SELECT: _native("acp"),
        Capability.PERMISSION_SET: _unsupported(
            "agy exposes --dangerously-skip-permissions and ACP mode, not an allow/deny tool list; adapter seam reserved"
        ),
    },
    ("agy", False): {
        Capability.INTERACTIVE_ATTACH: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.WAKE_PUSH: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.PROACTIVE_SEND: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.SESSION_LIFECYCLE: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.TOOL_INJECTION: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.PLUGIN_INJECTION: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.TURN_CONTROL: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.EVENT_STREAM: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.MODEL_SELECT: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
        Capability.PERMISSION_SET: _unsupported(
            "agy interactive TUI attach is deferred to phase 2 after cell-org I5-3"
        ),
    },
}

_CONCURRENCY: dict[tuple[str, bool], HarnessConcurrency] = {
    ("jev", True): HarnessConcurrency("pool", 10),
    ("jev", False): HarnessConcurrency("pool", 10),
}

def worker_spec(
    specs: Iterable[HarnessLaunchSpec], name: str, *, excluding: str | None = None
) -> HarnessLaunchSpec | None:
    """Resolve one worker, never an adapter or an ambiguous legacy name."""
    matches = [spec for spec in specs if spec.name == name
               and spec.harness in DECLARED_HARNESSES and spec.harness != excluding]
    return matches[0] if len(matches) == 1 else None


def declare(harness: str, *, headless: bool) -> dict[Capability, CapabilitySupport]:
    """The capability row for one (harness, mode) pair.

    Raises ``KeyError`` for a harness with no declared row (lark is an
    adapter, not a harness, and is deliberately absent tonight).
    """

    return dict(_DECLARATIONS[(harness, headless)])


def support(
    harness: str, *, headless: bool, capability: Capability
) -> CapabilitySupport | None:
    """One capability's support, or None for an unknown kind/mode/capability."""

    return _DECLARATIONS.get((harness, headless), {}).get(capability)


def concurrency(harness: str, *, headless: bool) -> HarnessConcurrency:
    """Return the fixed admission declaration for one harness kind."""

    try:
        return _CONCURRENCY[(harness, headless)]
    except KeyError:
        # Existing runtimes have the serial ``StreamingTurnProcess`` contract;
        # keeping them out of this table avoids inventing a second concurrency
        # declaration for legacy workers.
        raise KeyError((harness, headless)) from None
