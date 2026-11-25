from __future__ import annotations
from collections.abc import Callable
from pathlib import Path
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
)
from hyprial.daemon.impl.inbox.service  import InboxService
from hyprial.daemon.impl.inbox.service.policy  import RetryPolicy

class _NoStateActorIo:
    """Tripwire: state-owned service code must never cross a transport seam."""

    @staticmethod
    def _forbidden() -> None:
        raise RuntimeError("external I/O attempted on the delivery state actor")

    def is_online(self, recipient: str) -> bool:
        del recipient
        self._forbidden()

    def deliver(self, message: InboxMessage) -> bool:
        del message
        self._forbidden()

    def online_mailboxes(self) -> tuple[str, ...]:
        self._forbidden()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        del mailbox, message
        self._forbidden()

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        del node, message
        self._forbidden()
class _ActorOwnedInboxService(InboxService):
    """Existing schema/state transitions with all failure delivery externalized."""

    def __init__(
        self,
        database: Path,
        *,
        failure_sink: Callable[[InboxMessage, str], None],
        retry_policy: RetryPolicy | None,
        max_inbox_items: int,
        max_custody_bytes: int,
        node_id: str,
        service_options: dict[str, object] | None,
    ) -> None:
        self._failure_sink = failure_sink
        options = dict(service_options or {})
        options.pop("alarm_human_delivery", None)
        super().__init__(
            database,
            _NoStateActorIo(),
            retry_policy=retry_policy,
            max_inbox_items=max_inbox_items,
            max_custody_bytes=max_custody_bytes,
            node_id=node_id,
            _serialized_by_actor=True,
            **options,
        )

    def _emit_failure(self, message: InboxMessage, reason: str) -> None:
        self._failure_sink(message, reason)
