"""Runtime cell terminal result settlement and next-hop handoff (I5-4).

Decision notes (I5-4):
- Consumes only ``CellDeliveryPort.next_hop_route`` on ``bridge._cell_delivery``
  and never imports ``cells.actor`` or constructs ``CellDispatchEnvelope``.
- ``refused`` and ``retired`` settle via ``inbox.settle_harness_failure`` with
  ``CELL_WORK_REFUSED`` / internal ``CELL_RETIRED`` without mutating
  ``delivery_status`` directly.
- ``empty`` cells keep work ``pending`` by releasing the origin claim without
  acknowledgement or refusal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
)
from hyprial.daemon.impl.inbox import InboxMessage
from hyprial.daemon.impl.runtime.settlement import (
    HARNESS_FAILURE_BACKOFF_MS,
    HARNESS_FAILURE_MAX_ATTEMPTS,
)

CELL_WORK_REFUSED = "CELL_WORK_REFUSED"
CELL_RETIRED = "CELL_RETIRED"
_TERMINAL_FAILURE_RETRY_ATTEMPTS = HARNESS_FAILURE_MAX_ATTEMPTS
_TERMINAL_FAILURE_BACKOFF = HARNESS_FAILURE_BACKOFF_MS


@dataclass(frozen=True, slots=True)
class CellHopState:
    """In-flight multi-hop cell delivery bound to its durable inbox row."""

    original: InboxMessage
    hop_index: int
    stamp: Any
    root_sender: str
    root_recipient: str


def _hop_lock(bridge: Any) -> threading.Lock:
    lock = getattr(bridge, "_cell_hop_lock", None)
    if lock is None:
        lock = threading.Lock()
        bridge._cell_hop_lock = lock
    return lock


def _active_hops(bridge: Any) -> dict[str, CellHopState]:
    hops = getattr(bridge, "_active_cell_hops", None)
    if hops is None:
        hops = {}
        bridge._active_cell_hops = hops
    return hops


def _held_origins(bridge: Any) -> set[str]:
    origins = getattr(bridge, "_held_cell_origins", None)
    if origins is None:
        origins = set()
        bridge._held_cell_origins = origins
    return origins


def pop_cell_hop(bridge: Any, delivery_id: str) -> CellHopState | None:
    """Remove and return the active cell hop state for ``delivery_id``."""

    with _hop_lock(bridge):
        state = _active_hops(bridge).pop(delivery_id, None)
        if state is not None:
            _held_origins(bridge).discard(state.original.message_id)
        return state


def release_held_cell_origin(bridge: Any, original: InboxMessage) -> None:
    """Clear held cell tracking when a routed delivery ends or interrupts."""

    with _hop_lock(bridge):
        _held_origins(bridge).discard(original.message_id)


def _settle_permanent_cell_code(
    bridge: Any,
    original: InboxMessage,
    result: HarnessResult,
    code: str,
) -> bool:
    try:
        bridge.inbox.settle_harness_failure(
            original.recipient,
            original.message_id,
            code,
            permanent=True,
            max_attempts=_TERMINAL_FAILURE_RETRY_ATTEMPTS,
            backoff_ms=_TERMINAL_FAILURE_BACKOFF,
            now_ms=bridge._clock_ms(),
        )
    except KeyError:
        bridge._forget_origin_claim_if_present(original.message_id)
        bridge._settle_delivery_hooks(result)
        return False
    bridge._forget_origin_claim_if_present(original.message_id)
    bridge._settle_delivery_hooks(result)
    if bridge._logger is not None:
        bridge._logger(
            "warn",
            "daemon",
            "harness.cell.terminal_settled",
            messageId=original.message_id,
            recipient=original.recipient,
            reason=code,
        )
    return True


def _next_hop_delivery(
    bridge: Any, original: InboxMessage, result: HarnessResult, hop_index: int
) -> HarnessDelivery:
    payload = result.output if result.output else bridge._message_text(original)
    origin_getter = getattr(bridge, "_stored_message_origin", None)
    origin = origin_getter(original) if callable(origin_getter) else {}
    return HarnessDelivery(
        delivery_id=f"{original.message_id}::hop::{hop_index}",
        conversation_id=original.conversation_id,
        sender=original.sender,
        recipient=original.recipient,
        message=payload,
        origin=origin,
    )


def _dispatch_hop_envelope(
    bridge: Any, envelope: Any, original: InboxMessage
) -> bool:
    submit = getattr(bridge, "_submit_envelope", None)
    if callable(submit):
        member = envelope.stamp.member
        return bool(submit(member, envelope, original=original, retry=False, notice=False))
    dispatch = getattr(bridge.harnesses, "dispatch", None)
    return callable(dispatch) and bool(dispatch(envelope))


def settle_cell_result(
    bridge: Any,
    original: InboxMessage,
    result: HarnessResult,
    *,
    hop_index: int = 0,
    stamp: Any = None,
) -> str | None:
    """Handle cell terminal routing or refusal; None falls through to normal done."""

    outcome = getattr(result, "route_outcome", "done")
    label = getattr(result, "route_label", None)
    if outcome == "refused":
        _settle_permanent_cell_code(bridge, original, result, CELL_WORK_REFUSED)
        return "settled"
    if outcome == "done":
        if label is not None:
            raise ValueError("route_label is only allowed when route_outcome is next")
        return None
    if outcome != "next":
        raise ValueError(f"unknown route_outcome {outcome!r}")
    if not isinstance(label, str) or not label or not label.strip():
        raise ValueError("route_label must be a non-empty string when route_outcome is next")
    port = getattr(bridge, "_cell_delivery", None)
    next_hop_route = getattr(port, "next_hop_route", None)
    if not callable(next_hop_route):
        bridge._release_origin_claims_if_present((original.message_id,))
        return "pending"
    candidate = _next_hop_delivery(bridge, original, result, hop_index + 1)
    route = next_hop_route(
        original.recipient,
        delivery=candidate,
        outcome=outcome,
        label=label,
        stamp=stamp,
        hop_index=hop_index,
    )
    if route.action == "done":
        return None
    if route.action == "pending":
        bridge._release_origin_claims_if_present((original.message_id,))
        return "pending"
    if route.action == "retired":
        _settle_permanent_cell_code(bridge, original, result, route.reason or CELL_RETIRED)
        return "settled"
    if route.action == "refused":
        _settle_permanent_cell_code(bridge, original, result, route.reason or CELL_WORK_REFUSED)
        return "settled"
    if route.action == "hop" and route.envelope is not None:
        hop_state = CellHopState(
            original=original,
            hop_index=route.envelope.hop_index,
            stamp=route.envelope.stamp,
            root_sender=original.sender,
            root_recipient=original.recipient,
        )
        hop_id = route.envelope.delivery_id
        with _hop_lock(bridge):
            _active_hops(bridge)[hop_id] = hop_state
            _held_origins(bridge).add(original.message_id)
        if _dispatch_hop_envelope(bridge, route.envelope, original):
            return "routed"
        pop_cell_hop(bridge, hop_id)
        bridge._release_origin_claims_if_present((original.message_id,))
        return "pending"
    bridge._release_origin_claims_if_present((original.message_id,))
    return "pending"


def normalize_root_result(original: InboxMessage, result: HarnessResult) -> HarnessResult:
    """Bind a completed multi-hop result back to the durable root delivery."""

    if result.delivery_id == original.message_id and result.recipient == original.recipient:
        return result
    return replace(result, delivery_id=original.message_id, recipient=original.recipient)


__all__ = [
    "CELL_RETIRED",
    "CELL_WORK_REFUSED",
    "CellHopState",
    "normalize_root_result",
    "pop_cell_hop",
    "release_held_cell_origin",
    "settle_cell_result",
]
