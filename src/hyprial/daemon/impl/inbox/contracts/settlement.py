"""The one settle-hint decision shared by both settlement ends (WP5-A §2.4).

The IPC handler (``application/messaging/inbox_surface/queries.py``) and the
headless bridge call exactly this function.  It reads authoritative row
metadata only: a carrier never reports its own ``intent``, and this module
never imports a harness.
"""

from __future__ import annotations

from collections.abc import Callable

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.links.ports import LinkGate, SettleHint

__all__ = ["settle_hint_for"]


def settle_hint_for(
    *,
    pending: InboxMessage | None,
    gate: LinkGate | None,
    actor: str,
    message_id: str,
    log: Callable[..., None] | None = None,
) -> SettleHint:
    """``ack``, ``keep`` or a gate verdict for one carried message.

    1. no row for the caller's keys -> ack (answered, acked, drained or
       someone else's message);
    2. a row that is not a request -> ack (ruling 56);
    3. no gate bound -> keep;
    4. else the gate decides; an unknown answer becomes keep and is logged.
    """

    if pending is None:
        return "ack"
    if pending.intent != "request":
        return "ack"
    if gate is None:
        return "keep"
    answer = gate.settle_hint(actor=actor, message_id=message_id)
    if answer in ("ack", "keep"):
        return answer
    if log is not None:
        log(
            "warn",
            "daemon",
            "message.settle_hint.invalid",
            messageId=message_id,
            hint=str(answer)[:64],
        )
    return "keep"
