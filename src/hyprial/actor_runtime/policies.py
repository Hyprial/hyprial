from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from hyprial.backoff import capped_exponential


STATE_AUTHORITY = "state_authority"
EXTERNAL_IO = "external_io"
PROCESS_LIFECYCLE = "process_lifecycle"


@dataclass(frozen=True, slots=True)
class SupervisionPolicy:
    max_restarts: int
    restart_window: float
    base_backoff: float
    max_backoff: float
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_restarts < 0:
            raise ValueError("max_restarts must not be negative")
        if self.restart_window <= 0:
            raise ValueError("restart_window must be positive")
        if self.base_backoff < 0 or self.max_backoff < self.base_backoff:
            raise ValueError("invalid backoff bounds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay(self, attempt: int, jitter_sample: float) -> float:
        nominal = min(
            self.max_backoff,
            capped_exponential(self.base_backoff, self.max_backoff, attempt - 1),
        )
        offset = nominal * self.jitter_ratio * ((2 * jitter_sample) - 1)
        return max(0.0, nominal + offset)


DEFAULT_POLICIES: Mapping[str, SupervisionPolicy] = MappingProxyType(
    {
        STATE_AUTHORITY: SupervisionPolicy(
            max_restarts=3,
            restart_window=60.0,
            base_backoff=0.1,
            max_backoff=2.0,
            jitter_ratio=0.1,
        ),
        EXTERNAL_IO: SupervisionPolicy(
            max_restarts=5,
            restart_window=60.0,
            base_backoff=0.25,
            max_backoff=10.0,
            jitter_ratio=0.2,
        ),
        PROCESS_LIFECYCLE: SupervisionPolicy(
            max_restarts=2,
            restart_window=120.0,
            base_backoff=0.5,
            max_backoff=5.0,
            jitter_ratio=0.1,
        ),
    }
)


def freeze_policy_catalog(
    policies: Mapping[str, SupervisionPolicy] | None = None,
) -> Mapping[str, SupervisionPolicy]:
    selected = dict(DEFAULT_POLICIES if policies is None else policies)
    if not selected:
        raise ValueError("policy catalog must not be empty")
    return MappingProxyType(selected)
