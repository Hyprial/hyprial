"""The one settle-hint decision shared by both settlement ends (WP5-A §2.4).

The IPC handler (``application/messaging/inbox_surface/queries.py``) and the
headless bridge call exactly this function.  It reads authoritative row
metadata only: a carrier never reports its own ``intent``, and this module
never imports a harness.

T1b (CM-PLAN「摘掉调用」): the gate parameter is gone.  The rule is fixed —
no row for the caller's keys -> ``ack``; a row that is not a request ->
``ack``; otherwise ``keep`` — which is also the answer the gate gave in the
default configuration whenever it was not draining reclaimed residue.
"""

from __future__ import annotations

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.kernel import SettleHint

__all__ = ["settle_hint_for"]


def settle_hint_for(
    *,
    pending: InboxMessage | None,
    actor: str,
    message_id: str,
) -> SettleHint:
    """``ack`` or ``keep`` for one carried message.

    1. no row for the caller's keys -> ack (answered, acked or someone
       else's message);
    2. a row that is not a request -> ack (ruling 56);
    3. otherwise keep.
    """

    if pending is None:
        return "ack"
    if pending.intent != "request":
        return "ack"
    return "keep"
