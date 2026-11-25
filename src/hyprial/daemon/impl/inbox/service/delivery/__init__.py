"""Inbox service delivery mixins."""
from hyprial.daemon.impl.inbox.service.delivery.receive import InboxServiceReceiveMixin
from hyprial.daemon.impl.inbox.service.delivery.retry import InboxServiceRetryMixin
from hyprial.daemon.impl.inbox.service.delivery.outbox import InboxServiceOutboxMixin
from hyprial.daemon.impl.inbox.service.delivery.notices import InboxServiceNoticesMixin
from hyprial.daemon.impl.inbox.service.delivery.system_notices import InboxServiceSystemNoticesMixin
from hyprial.daemon.impl.inbox.service.delivery.bodies import InboxServiceBodiesMixin


class InboxServiceDeliveryMixin(
    InboxServiceReceiveMixin,
    InboxServiceRetryMixin,
    InboxServiceOutboxMixin,
    InboxServiceNoticesMixin,
    InboxServiceSystemNoticesMixin,
    InboxServiceBodiesMixin,
):
    """Combined delivery behavior for InboxService."""
