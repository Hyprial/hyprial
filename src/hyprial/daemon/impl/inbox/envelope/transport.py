"""The sender stamping site: a network delivery transport decorator.

Decisions (cell model phase B, wire option B; merged plan §1.1):

* The inner envelope is written where a message leaves this node for the
  network, not where ``message.send`` builds the payload.  The stored outbox
  row, the sender copy, the body digests and the submission digest therefore
  keep the payload the caller authored, and the §1.1 pin "the submission
  digest never includes envelope fields" holds for the payload too: a retried
  submit of a pre-upgrade command still joins its receipt.
* ``deliver`` and ``transfer_custody`` carry the envelope.  ``deliver_notice``
  and ``deliver_progress`` keep their own payload schemas and pass through.
  Every other question goes to the inner transport unchanged.
* The decorator wraps only the frame-① network transport.  Same-node delivery
  never encodes a frame and hands the message, envelope fields included, over
  in process.
"""

from __future__ import annotations

from hyprial.daemon.impl.inbox.contracts.api import DeliveryTransport, InboxMessage
from hyprial.daemon.impl.inbox.envelope.payload import stamp_payload


class EnvelopeStampingTransport:
    """A network transport whose frames carry the sender's payload envelope."""

    def __init__(self, inner: DeliveryTransport) -> None:
        self._inner = inner

    def is_online(self, recipient: str) -> bool:
        return self._inner.is_online(recipient)

    def online_mailboxes(self) -> tuple[str, ...]:
        return self._inner.online_mailboxes()

    def ack_received(self, message: InboxMessage) -> bool:
        confirm = getattr(self._inner, "ack_received", None)
        return bool(confirm(message)) if callable(confirm) else False

    def deliver(self, message: InboxMessage) -> bool:
        return self._inner.deliver(stamp_payload(message))

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return self._inner.transfer_custody(mailbox, stamp_payload(message))

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        return self._inner.deliver_notice(node, message)

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        return self._inner.deliver_progress(node, message)


__all__ = ["EnvelopeStampingTransport"]
