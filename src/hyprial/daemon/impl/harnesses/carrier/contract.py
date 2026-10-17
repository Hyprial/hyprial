"""Harness-neutral carrier contract (design §8.1, WP5-A §2).

A carrier is any adapter that puts a pending inbox row in front of an agent
turn.  This module owns the three things every carrier shares, and nothing
about a particular harness:

* how a pending row becomes an injection block (``render_injection``) --
  summary only, never the body;
* the four calls the daemon answers a carrier with (``CarrierPort``);
* the turn IPC method names carriers report on.

The one place that decides ``ack`` versus ``keep`` is
``inbox.contracts.settlement.settle_hint_for``, not this module: the message
authority must not depend on the carrier package.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.contracts.summary import body_bytes, body_text
from hyprial.daemon.impl.links.ports import IPC_MESSAGE_SETTLE_HINT, SettleHint

__all__ = [
    "CarrierPort",
    "INJECTION_FORMAT",
    "INJECTION_KINDS",
    "IPC_SESSION_TURN_ENDED",
    "IPC_SESSION_TURN_STARTED",
    "Injection",
    "IpcCarrierPort",
    "SettleOutcome",
    "TURN_END_STATUSES",
    "injection_from_inbox",
    "injection_from_row",
    "render_injection",
    "settle_injected",
]

IPC_SESSION_TURN_STARTED = "session.turn.started"
IPC_SESSION_TURN_ENDED = "session.turn.ended"
IPC_MESSAGE_ACK = "message.ack"
TURN_END_STATUSES = frozenset({"completed", "failed"})
INJECTION_KINDS = frozenset({"message", "notice"})
#: Persisted injection text version (the codex store records this string).
INJECTION_FORMAT = "summary-v1"

_NO_SUMMARY = "(none; the sender gave no summary)"


@dataclass(frozen=True, slots=True)
class Injection:
    """One rendered-safe view of a pending row: fields allowed in context."""

    message_id: str
    sender: str
    recipient: str
    intent: str
    kind: str
    summary: str | None
    body_bytes: int


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def injection_from_row(row: Mapping[str, Any]) -> Injection | None:
    """Read only the §8.1 fields; never the body, payload or topic."""

    message_id = row.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        return None
    kind = row.get("kind")
    if kind not in INJECTION_KINDS:
        kind = "message"
    summary = row.get("summary")
    measured = row.get("bodyBytes")
    return Injection(
        message_id=message_id,
        sender=_text(row.get("from")),
        recipient=_text(row.get("to")),
        intent=_text(row.get("intent")),
        kind=kind,
        summary=summary if isinstance(summary, str) else None,
        body_bytes=measured if isinstance(measured, int) and not isinstance(measured, bool) else 0,
    )


def injection_from_inbox(message: InboxMessage, *, notice: bool = False) -> Injection:
    """Build an injection from a durable row.  A notice carries its own text."""

    if notice:
        return Injection(
            message_id=message.message_id,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            kind="notice",
            summary=body_text(message),
            body_bytes=0,
        )
    return Injection(
        message_id=message.message_id,
        sender=message.sender,
        recipient=message.recipient,
        intent=message.intent,
        kind="message",
        summary=message.summary,
        body_bytes=body_bytes(message),
    )


def render_injection(item: Injection) -> str:
    """The exact block a model sees.  The body is never inlined."""

    if item.kind == "notice":
        return f"notice: {item.summary or ''}"
    summary = item.summary if item.summary else _NO_SUMMARY
    message_id = item.message_id
    return (
        f"summary: {summary}\n"
        f"(intent: {item.intent}, kind: message, messageId: {message_id}, "
        f"bodyBytes: {item.body_bytes})\n"
        f'Full body: harness_read_body("{message_id}") or `hyprial read {message_id}`. '
        f'Reply with harness_reply("{message_id}", …); if no reply is needed, '
        f'harness_ack("{message_id}").'
    )


class CarrierPort(Protocol):
    """The four calls a carrier makes to settlement and turn reporting."""

    def settle_hint(self, message_id: str) -> SettleHint: ...

    def ack(self, message_id: str) -> bool:
        """True when the row is acked or already gone (ACK_UNAVAILABLE)."""
        ...

    def turn_started(self, report_id: str, *, delivery_id: str = "") -> None: ...

    def turn_ended(self, report_id: str, *, status: str, delivery_id: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class SettleOutcome:
    acked: tuple[str, ...]
    kept: tuple[str, ...]
    deferred: tuple[str, ...]


def settle_injected(
    port: CarrierPort,
    message_ids: Sequence[str],
    *,
    already: Collection[str] = (),
) -> SettleOutcome:
    """Ask each injected id for its hint and do exactly that.  Never replies."""

    acked: list[str] = []
    kept: list[str] = []
    deferred: list[str] = []
    for message_id in message_ids:
        if message_id in already:
            continue
        try:
            hint = port.settle_hint(message_id)
        except Exception:  # noqa: BLE001 - one failed id must not stop the rest
            deferred.append(message_id)
            continue
        if hint != "ack":
            kept.append(message_id)
            continue
        try:
            if port.ack(message_id):
                acked.append(message_id)
            else:
                deferred.append(message_id)
        except Exception:  # noqa: BLE001 - transport failure defers this id
            deferred.append(message_id)
    return SettleOutcome(tuple(acked), tuple(kept), tuple(deferred))


class IpcCarrierPort:
    """A Python carrier's ``CarrierPort`` over the daemon IPC seam.

    ``request(method, params)`` performs one IPC call; ``signed(params)``
    adds the caller's fence (``sessionRef`` and any other credential) so no
    request ever travels unsigned.
    """

    def __init__(
        self,
        request: Callable[[str, dict[str, Any]], Mapping[str, Any]],
        signed: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._request = request
        self._signed = signed

    def settle_hint(self, message_id: str) -> SettleHint:
        result = self._request(
            IPC_MESSAGE_SETTLE_HINT, self._signed({"messageId": message_id})
        )
        return result["hint"]  # type: ignore[return-value]

    def ack(self, message_id: str) -> bool:
        try:
            result = self._request(
                IPC_MESSAGE_ACK, self._signed({"messageId": message_id})
            )
        except DaemonRequestError as error:
            if error.code == ipc_errors.MESSAGE_ACK_UNAVAILABLE:
                return True
            raise
        return bool(result.get("acknowledged"))

    def turn_started(self, report_id: str, *, delivery_id: str = "") -> None:
        params: dict[str, Any] = {"reportId": report_id}
        if delivery_id:
            params["deliveryId"] = delivery_id
        self._request(IPC_SESSION_TURN_STARTED, self._signed(params))

    def turn_ended(self, report_id: str, *, status: str, delivery_id: str = "") -> None:
        params: dict[str, Any] = {"reportIds": [report_id], "status": status}
        if delivery_id:
            params["deliveryId"] = delivery_id
        self._request(IPC_SESSION_TURN_ENDED, self._signed(params))
