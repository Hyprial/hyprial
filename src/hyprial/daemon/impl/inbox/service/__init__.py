"""Durable inbox SQLite state machine."""
from hyprial.daemon.impl.inbox.service.core import InboxServiceCoreMixin
from hyprial.daemon.impl.inbox.service.delivery import InboxServiceDeliveryMixin
from hyprial.daemon.impl.inbox.service.link_ledger import InboxServiceLinkLedgerMixin
from hyprial.daemon.impl.inbox.service.projection import InboxServiceProjectionMixin
from hyprial.daemon.impl.inbox.service.state import ConsumptionState
from hyprial.daemon.impl.inbox.service.policy import RetryPolicy
from hyprial.daemon.impl.inbox.service.delivery.notices import _ExpiredSenderNotice, _bare_sender


class InboxService(
    InboxServiceCoreMixin,
    InboxServiceLinkLedgerMixin,
    InboxServiceDeliveryMixin,
    InboxServiceProjectionMixin,
):
    """Durable inbox SQLite state machine."""


__all__ = ["InboxService", "ConsumptionState", "RetryPolicy", "_ExpiredSenderNotice", "_bare_sender"]
