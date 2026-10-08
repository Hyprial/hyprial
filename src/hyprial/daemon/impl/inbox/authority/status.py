from __future__ import annotations
from typing import TYPE_CHECKING
from hyprial.daemon.impl.inbox.links.pull  import DeliveryStatus

if TYPE_CHECKING:
    from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection

class DeliveryStatusProjection:
    def __init__(self, reads: InboxReadProjection) -> None:
        self._reads = reads

    def for_sender(
        self,
        sender: str,
        *,
        message_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DeliveryStatus, ...]:
        return self._reads.delivery_status_records(
            sender,
            message_id=message_id,
            limit=limit,
        )
