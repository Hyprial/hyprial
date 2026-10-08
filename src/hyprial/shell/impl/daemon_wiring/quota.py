"""Business quota evaluation; daemon still owns persistence and delivery."""

from hyprial.biz import QuotaWatchdog
from hyprial.daemon import QuotaWatchdogDeps


class _QuotaEvaluator(QuotaWatchdog):
    def _save(self):
        # Evaluation mutates a tentative in-memory state. The daemon commits
        # its durable outbox and state only after notification settlement.
        pass


def quota_evaluator_factory(deps: QuotaWatchdogDeps):
    return _QuotaEvaluator(
        state_dir=deps.state_dir, deliver=deps.deliver,
        readings=deps.readings, clock_ms=deps.clock_ms,
    )
