"""The neutral hook event envelope and its canonical names.

The daemon emits facts here; it never encodes policy.  Sources stamp their own
``seq`` (per-source FIFO); the bus neither re-stamps nor orders across sources.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

HOOK_EVENT_SCHEMA_VERSION = 2
TURN_EXCERPT_CHARS = 1_000

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(
    r"(?i)\b((?:authorization\s*:\s*)?bearer\s+)([^\s,;]+)"
)
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"ghp_[A-Za-z0-9_-]{6,}|"
    r"github_pat_[A-Za-z0-9_-]{6,}|"
    r"xoxb-[A-Za-z0-9_-]{6,}|"
    r"AKIA[A-Z0-9]{16}"
    r")\b"
)
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:])(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+|[A-Za-z]:\\[^\s]+)"
)

#: The stable name contract other lines align to (design §3.2).
EVENT_NAMES: frozenset[str] = frozenset(
    {
        "actor.up",
        "actor.down",
        "actor.lost",
        "actor.restored",
        "actor.unowned",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "delivery.prepared",
        "delivery.dispatched",
        "delivery.settled",
        "delivery.held",
        "graph.finished",
        "graph.closed",
        "worker.cleanup.complete",
    }
)

#: The only event an intercept hook may bind this release (design §4.2).
INTERCEPT_EVENT_NAMES: frozenset[str] = frozenset({"delivery.prepared"})

HookKind = Literal["observe", "intercept"]
TurnFidelity = Literal["observed", "inferred"]


def redact_turn_excerpt(
    value: str, *, enforce_output_bound: bool = True
) -> str:
    """Apply the existing neutral turn-record redaction and size bound."""

    excerpt = value[:TURN_EXCERPT_CHARS]
    excerpt = _BEARER_TOKEN.sub(
        lambda match: f"{match.group(1)}<redacted>", excerpt
    )
    excerpt = _KNOWN_TOKEN.sub("<redacted>", excerpt)
    excerpt = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=<redacted>", excerpt
    )
    excerpt = _ABSOLUTE_PATH.sub("<path>", excerpt)
    return excerpt[:TURN_EXCERPT_CHARS] if enforce_output_bound else excerpt


def safe_turn_identifier(value: str) -> str:
    return redact_turn_excerpt(value)[:300]


def safe_turn_tool_names(values: tuple[str, ...]) -> list[str]:
    output: list[str] = []
    for value in values:
        candidate = value.strip()[:100]
        if not candidate or "/" in candidate or "\\" in candidate:
            continue
        if candidate not in output:
            output.append(candidate)
    return output


def turn_event_payload(
    *,
    delivery_id: str,
    sender: str,
    conversation: str,
    started_at_ms: int,
    ended_at_ms: int,
    status: str,
    tool_names: tuple[str, ...],
    output: str,
    prompt: str,
    tool_calls: tuple[tuple[str, bool], ...],
    fidelity: TurnFidelity,
    failure_code: str | None = None,
) -> Mapping[str, Any]:
    """Build the schema-v2 neutral payload shared by all ``turn.*`` events."""

    if fidelity not in ("observed", "inferred"):
        raise ValueError("turn event fidelity must be observed or inferred")
    names = safe_turn_tool_names(tool_names)
    calls: list[dict[str, object]] = []
    for name, ok in tool_calls:
        safe = safe_turn_tool_names((name,))
        if safe:
            calls.append({"name": safe[0], "ok": bool(ok)})
    payload: dict[str, Any] = {
        "deliveryId": safe_turn_identifier(delivery_id),
        "sender": safe_turn_identifier(sender),
        "conversation": safe_turn_identifier(conversation),
        "startedAtMs": int(started_at_ms),
        "endedAtMs": int(ended_at_ms),
        "status": str(status),
        "toolNames": names,
        "outputExcerpt": redact_turn_excerpt(output),
        "promptExcerpt": redact_turn_excerpt(prompt),
        "toolCalls": calls,
        "collectionMethod": "daemon",
        "fidelity": fidelity,
    }
    if failure_code is not None:
        payload["failureCode"] = safe_turn_identifier(failure_code)
    return MappingProxyType(payload)


@dataclass(frozen=True, slots=True)
class HookEvent:
    event: str
    kind: HookKind
    emitted_at_ms: int
    seq: int
    source: str
    actor: str | None = None
    graph_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = HOOK_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != HOOK_EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported hook event schema {self.schema_version!r}")
        if self.event not in EVENT_NAMES:
            raise ValueError(f"unknown hook event {self.event!r}")
        if self.kind not in ("observe", "intercept"):
            raise ValueError(f"unknown hook kind {self.kind!r}")
        if self.kind == "intercept" and self.event not in INTERCEPT_EVENT_NAMES:
            raise ValueError(f"{self.event!r} cannot be intercepted")
        if not self.source.strip():
            raise ValueError("hook event source must not be blank")
        if self.seq < 0 or self.emitted_at_ms < 0:
            raise ValueError("hook event seq and emitted_at_ms must not be negative")
        # Observers on other threads read the payload; freeze a private copy.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


__all__ = [
    "EVENT_NAMES",
    "HOOK_EVENT_SCHEMA_VERSION",
    "INTERCEPT_EVENT_NAMES",
    "TURN_EXCERPT_CHARS",
    "HookEvent",
    "HookKind",
    "TurnFidelity",
    "redact_turn_excerpt",
    "safe_turn_identifier",
    "safe_turn_tool_names",
    "turn_event_payload",
]
