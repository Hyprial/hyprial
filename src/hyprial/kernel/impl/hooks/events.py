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
        "usage.refreshed",
        "worker.cleanup.complete",
    }
)

GRAPH_END_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

#: The only event an intercept hook may bind this release (design §4.2).
INTERCEPT_EVENT_NAMES: frozenset[str] = frozenset({"delivery.prepared"})

GraphEndState = Literal["completed", "failed", "cancelled"]
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


def delivery_event_payload(
    *,
    delivery_id: str,
    sender: str,
    conversation: str,
    message: str | None = None,
    outcome: str | None = None,
    failure_code: str | None = None,
) -> Mapping[str, Any]:
    """Build the schema-v2 neutral payload shared by all ``delivery.*`` events.

    ``delivery.prepared`` carries ``messageExcerpt`` (redacted, bounded like
    turn excerpts); ``delivery.dispatched`` carries identifiers only;
    ``delivery.settled`` adds ``outcome`` (the turn's terminal status) and,
    for failed turns, ``failureCode``.  ``sender`` and ``conversation`` are
    empty strings when the emitting daemon no longer knows them (a settlement
    after restart).
    """

    payload: dict[str, Any] = {
        "deliveryId": safe_turn_identifier(delivery_id),
        "sender": safe_turn_identifier(sender),
        "conversation": safe_turn_identifier(conversation),
        "collectionMethod": "daemon",
        "fidelity": "observed",
    }
    if message is not None:
        payload["messageExcerpt"] = redact_turn_excerpt(message)
    if outcome is not None:
        payload["outcome"] = str(outcome)
    if failure_code is not None:
        payload["failureCode"] = safe_turn_identifier(failure_code)
    return MappingProxyType(payload)


def usage_refreshed_payload() -> Mapping[str, Any]:
    """Payload for ``usage.refreshed``: a tick, not a data carrier.

    The event says the daemon's usage cache finished a refresh; consumers
    read the current readings from the cache themselves.  Provenance labels
    follow design §2.2: the daemon observed the refresh directly.
    """

    return MappingProxyType(
        {"collectionMethod": "daemon", "fidelity": "observed"}
    )


def _hook_fidelity(fidelity: TurnFidelity) -> str:
    if fidelity not in ("observed", "inferred"):
        raise ValueError("hook event fidelity must be observed or inferred")
    return fidelity


def graph_closed_event_payload(
    *,
    graph_id: str,
    end_state: GraphEndState | None,
    closed_by: str | None,
    reason_ref: str | None,
    closed_at_ms: int,
    fidelity: TurnFidelity = "observed",
) -> Mapping[str, Any]:
    """Build the schema-v2 payload for one ``graph.closed`` transition.

    ``end_state`` is null for an unmanaged graph closed by its end flag: that
    path records no state, and none is invented.  ``reason_ref`` is null when
    absent (never the empty string).
    """

    if end_state is not None and end_state not in GRAPH_END_STATES:
        raise ValueError(
            "graph.closed end_state must be completed, failed, cancelled or None"
        )
    return MappingProxyType(
        {
            "graphId": safe_turn_identifier(graph_id),
            "endState": end_state,
            "closedBy": (
                None if closed_by is None else safe_turn_identifier(closed_by)
            ),
            "reasonRef": (
                None if not reason_ref else safe_turn_identifier(reason_ref)
            ),
            "closedAtMs": int(closed_at_ms),
            "collectionMethod": "daemon",
            "fidelity": _hook_fidelity(fidelity),
        }
    )


def worker_cleanup_complete_event_payload(
    *,
    graph_id: str,
    actor_node: str,
    actor_name: str,
    operation_id: str,
    at_ms: int,
    worker_state: str = "stopped",
    fidelity: TurnFidelity = "observed",
) -> Mapping[str, Any]:
    """Build the schema-v2 payload for one ``worker.cleanup.complete``."""

    return MappingProxyType(
        {
            "graphId": safe_turn_identifier(graph_id),
            "actorNode": safe_turn_identifier(actor_node),
            "actorName": safe_turn_identifier(actor_name),
            "operationId": safe_turn_identifier(operation_id),
            "workerState": str(worker_state),
            "atMs": int(at_ms),
            "collectionMethod": "daemon",
            "fidelity": _hook_fidelity(fidelity),
        }
    )


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
    "GRAPH_END_STATES",
    "HOOK_EVENT_SCHEMA_VERSION",
    "INTERCEPT_EVENT_NAMES",
    "TURN_EXCERPT_CHARS",
    "HookEvent",
    "HookKind",
    "GraphEndState",
    "TurnFidelity",
    "delivery_event_payload",
    "redact_turn_excerpt",
    "safe_turn_identifier",
    "safe_turn_tool_names",
    "turn_event_payload",
    "usage_refreshed_payload",
    "graph_closed_event_payload",
    "worker_cleanup_complete_event_payload",
]
