from __future__ import annotations
from uuid import NAMESPACE_URL, uuid5
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    SubmitProgressCommand,
)
from hyprial.daemon.impl.inbox.tracking.progress  import PROGRESS_INTENT, encode_progress_event

from ..events import (
    DispatchIoKind,
    DispatchIoRequested,
    DispatchItem,
)

class DeliveryCustodyProgressMixin:
    def _submit_progress(self, command: SubmitProgressCommand) -> None:
        event = command.event
        message = InboxMessage(
            message_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial-progress:{event.delivery_id}:{event.seq}:{event.phase}",
                )
            ),
            conversation_id=event.conversation_id,
            sender=event.actor,
            recipient=command.recipient,
            payload=encode_progress_event(event),
            intent=PROGRESS_INTENT,
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"progress:{event.delivery_id}:{event.seq}",
            created_at_ms=event.emitted_at_ms,
        )
        target_node = self._service._agent_node(command.recipient)
        if target_node is None or target_node == self._service.node_id:
            self._publish_bool(
                command.correlation_id,
                "submit_progress_event",
                self._service.receive_progress_event(message),
            )
            return
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=self._version,
            kind=DispatchIoKind.PROGRESS,
            items=(DispatchItem(message=message, target_node=target_node),),
        )
        self._request_io(request, completion_kind="progress")

