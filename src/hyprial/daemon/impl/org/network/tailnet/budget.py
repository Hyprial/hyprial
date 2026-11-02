"""A join has one deadline shared by membership, redial and OrgFS waits."""
from __future__ import annotations

import time
from collections.abc import Callable

from hyprial.kernel import MEMBERSHIP_BUDGETS, ipc_errors


class JoinBudget:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.monotonic
        self.seconds = MEMBERSHIP_BUDGETS["join"].seconds
        self.deadline = self.clock() + self.seconds

    def remaining(self, leg: str, maximum: float | None = None) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            self.expired(leg)
        return remaining if maximum is None else min(maximum, remaining)

    def expired(self, leg: str) -> None:
        from ..service import OrgNetworkError

        raise OrgNetworkError(
            ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            f"Org join leg {leg} exhausted the {self.seconds:g}s join budget.",
            {"operation": "join", "leg": leg, "budgetSeconds": self.seconds},
        )
