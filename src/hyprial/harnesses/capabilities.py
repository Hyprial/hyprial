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

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Capability(StrEnum):
    INTERACTIVE_ATTACH = "interactive_attach"  # TUI attach + daemon registration
    HEADLESS_EXEC = "headless_exec"  # managed delivery -> one turn -> result
    WAKE_PUSH = "wake_push"  # daemon can wake a live session
    PROACTIVE_SEND = "proactive_send"  # in-session agent can send out (harness_send)
    SESSION_LIFECYCLE = "session_lifecycle"  # fixed session id / resume / fork
    TOOL_INJECTION = "tool_injection"  # hyprial toolset injected and pre-authorized
    PLUGIN_INJECTION = "plugin_injection"  # HYPRIAL_HOME session plugin payloads
    TURN_CONTROL = "turn_control"  # interrupt (steer/follow_up optional supersets)
    EVENT_STREAM = "event_stream"  # structured events (completion, observability)
    MODEL_SELECT = "model_select"  # provider+model and endpoint selection


class SupportLevel(StrEnum):
    NATIVE = "native"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


PLUGIN_KINDS = frozenset({"mcp", "skill", "pi-extension", "claude-plugin"})


@dataclass(frozen=True, slots=True)
class CapabilitySupport:
    level: SupportLevel
    mechanism: str | None = None  # channel | agent_sdk | rpc | app_server | pty
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
        "dsh_api",
        "pty",
        "extension",
        "python_worker",
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
        Capability.MODEL_SELECT: _native("pty"),
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
    },
    ("codex", False): {
        Capability.INTERACTIVE_ATTACH: _native("app_server"),
        Capability.WAKE_PUSH: _native("app_server"),
        # Interactive Codex has no injected Harness MCP surface: the carrier
        # can deliver an inbound turn and auto-reply, but the model cannot
        # proactively address a new target from this session.
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _degraded(
            "app_server",
            "PR1 owns the current app-server/TUI launch; daemon-restart recovery lands in PR2",
        ),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _degraded(
            "app_server",
            "session-scoped MCP servers are supported; skills and extensions are not",
            plugin_kinds=frozenset({"mcp"}),
        ),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.MODEL_SELECT: _native("app_server"),
    },
    ("dsh", True): {
        Capability.HEADLESS_EXEC: _native("dsh_api"),
        Capability.WAKE_PUSH: _native("dsh_api"),
        Capability.PROACTIVE_SEND: _native("dsh_api"),
        Capability.SESSION_LIFECYCLE: _degraded(
            "dsh_api",
            "managed sessions are deliberately not resumed across daemon restart: "
            "the connector mints a fresh worker MCP identity per launch and refuses "
            "to attach it to an existing DSH session (dsh.py), so a restarted worker "
            "starts a new session",
        ),
        Capability.TOOL_INJECTION: _native("dsh_api"),
        Capability.TURN_CONTROL: _native("dsh_api"),
        Capability.EVENT_STREAM: _degraded(
            "dsh_api", "turn events are polled from session.history"
        ),
        Capability.MODEL_SELECT: _native("dsh_api"),
    },
    ("dsh", False): {
        Capability.INTERACTIVE_ATTACH: _unsupported(),
        Capability.WAKE_PUSH: _unsupported(),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported(),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _unsupported(),
        Capability.MODEL_SELECT: _unsupported(),
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
    },
    ("jev", False): {
        Capability.INTERACTIVE_ATTACH: _unsupported(),
        Capability.WAKE_PUSH: _unsupported(),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported(),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _unsupported(),
        Capability.MODEL_SELECT: _unsupported(),
    },
    ("user-proxy", True): {
        Capability.HEADLESS_EXEC: _unsupported(
            "user-proxy handler contract is not defined"
        ),
        Capability.WAKE_PUSH: _unsupported(
            "user-proxy handler contract is not defined"
        ),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported(),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _unsupported(),
        Capability.MODEL_SELECT: _unsupported(),
    },
    ("user-proxy", False): {
        Capability.INTERACTIVE_ATTACH: _unsupported(),
        Capability.WAKE_PUSH: _unsupported(),
        Capability.PROACTIVE_SEND: _unsupported(),
        Capability.SESSION_LIFECYCLE: _unsupported(),
        Capability.TOOL_INJECTION: _unsupported(),
        Capability.PLUGIN_INJECTION: _unsupported(),
        Capability.TURN_CONTROL: _unsupported(),
        Capability.EVENT_STREAM: _unsupported(),
        Capability.MODEL_SELECT: _unsupported(),
    },
}

_CONCURRENCY: dict[tuple[str, bool], HarnessConcurrency] = {
    ("jev", True): HarnessConcurrency("pool", 10),
    ("jev", False): HarnessConcurrency("pool", 10),
    ("user-proxy", True): HarnessConcurrency("sequential", 1),
    ("user-proxy", False): HarnessConcurrency("sequential", 1),
}

DECLARED_HARNESSES = frozenset({harness for harness, _ in _DECLARATIONS})


def declare(harness: str, *, headless: bool) -> dict[Capability, CapabilitySupport]:
    """The capability row for one (harness, mode) pair.

    Raises ``KeyError`` for a harness with no declared row (lark is an
    adapter, not a harness, and is deliberately absent tonight).
    """

    return dict(_DECLARATIONS[(harness, headless)])


def support(
    harness: str, *, headless: bool, capability: Capability
) -> CapabilitySupport | None:
    """One capability's support, or None when the mode does not apply."""

    return _DECLARATIONS[(harness, headless)].get(capability)


def concurrency(harness: str, *, headless: bool) -> HarnessConcurrency:
    """Return the fixed admission declaration for one harness kind."""

    try:
        return _CONCURRENCY[(harness, headless)]
    except KeyError:
        # Existing runtimes have the serial ``StreamingTurnProcess`` contract;
        # keeping them out of this table avoids inventing a second concurrency
        # declaration for legacy workers.
        raise KeyError((harness, headless)) from None
