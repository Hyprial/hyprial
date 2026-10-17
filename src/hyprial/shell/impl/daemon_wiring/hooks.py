"""Named business hook consumers registered by the shell composition root."""

from hyprial.biz import RecapHookConsumer, ReplyReminderConsumer
from hyprial.daemon import HookConsumersDeps


def hook_consumers(deps: HookConsumersDeps):
    """Build the daemon bus consumers from daemon-owned runtime inputs.

    The factory mirrors the quota seam: the daemon may not import biz, so it
    calls this with :class:`HookConsumersDeps` when it owns the bus and the
    agent-home resolver (unified-hooks design §2, H2).
    """

    return {
        "recap": RecapHookConsumer(
            home_for_actor=deps.home_for_agent,
            logger=deps.logger,
        )
    }


def reply_reminder_consumer_factory(port):
    """Wrap the daemon's reminder port as the biz bus consumer (WP7 §1.3).

    The logger comes from the port itself, so the shell needs no second deps
    object for the reminder lane.
    """

    return ReplyReminderConsumer(port=port, logger=port.log)


__all__ = ["hook_consumers", "reply_reminder_consumer_factory"]
