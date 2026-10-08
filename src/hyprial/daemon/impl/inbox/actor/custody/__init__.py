"""DeliveryCustody mixins."""
from hyprial.daemon.impl.inbox.actor.custody.lifecycle import DeliveryCustodyLifecycleMixin
from hyprial.daemon.impl.inbox.actor.custody.submit import DeliveryCustodySubmitMixin
from hyprial.daemon.impl.inbox.actor.custody.outcomes import DeliveryCustodyOutcomesMixin
from hyprial.daemon.impl.inbox.actor.custody.retry import DeliveryCustodyRetryMixin
from hyprial.daemon.impl.inbox.actor.custody.notices import DeliveryCustodyNoticesMixin
from hyprial.daemon.impl.inbox.actor.custody.progress import DeliveryCustodyProgressMixin


class DeliveryCustody(
    DeliveryCustodyLifecycleMixin,
    DeliveryCustodySubmitMixin,
    DeliveryCustodyOutcomesMixin,
    DeliveryCustodyRetryMixin,
    DeliveryCustodyNoticesMixin,
    DeliveryCustodyProgressMixin,
):
    """One actor generation owning every durable delivery mutation it accepts."""
