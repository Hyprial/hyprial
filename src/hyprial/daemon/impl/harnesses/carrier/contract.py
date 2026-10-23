"""Harness-neutral carrier contract (design §8.1, WP5-A §2).

A carrier is any adapter that puts a pending inbox row in front of an agent
turn.  This module owns the three things every carrier shares, and nothing
about a particular harness:

* the four calls the daemon answers a carrier with (``CarrierPort``);
* the turn IPC method names carriers report on.

The one place that decides ``ack`` versus ``keep`` is
``inbox.contracts.settlement.settle_hint_for``, not this module: the message
authority must not depend on the carrier package.

There is deliberately no renderer here any more.  All model-facing batch text
comes from ``daemon.impl.inbox.batch`` (``inbox-batch-v1``); a connector that
formats its own prompt would be a second presentation authority.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from hyprial.kernel import DaemonRequestError, ipc_errors

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
    "settle_injected",
]

IPC_SESSION_TURN_STARTED = "session.turn.started"
IPC_SESSION_TURN_ENDED = "session.turn.ended"
IPC_MESSAGE_ACK = "message.ack"
TURN_END_STATUSES = frozenset({"completed", "failed"})
INJECTION_KINDS = frozenset({"message", "notice"})
#: Persisted scalar injection text version (the codex store records it for
#: pre-batch rows).  A batch row records ``inbox-batch-v1`` instead.
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
