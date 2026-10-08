"""The minimal connector readiness report — daemon readiness phase ③.

③ is an event, not a state: "did this round disposition every desired
connector once".  The readiness boundary is the connector actor being
established and handing back its first report; whether the connector then
brings a harness up or reaches its external service (network, quota) is the
connector's own event-and-retry loop and never enters the daemon's readiness
observation.  The daemon asks exactly two questions of a report — did it
arrive, and what verdict did it carry — and forwards the verdict verbatim.
Failure and success must look different; ``verdict`` is what carries the
difference, so aggregation can count them apart without interpreting content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Phases a report can describe.  Only "connector-up" exists today — the
# connector actor was established and its first disposition settled.  Later
# phases extend the set without a schema change.
READINESS_PHASES: frozenset[str] = frozenset({"connector-up"})

# The verdict vocabulary, aligned with the retrying/failed split:
#   ready    — the start settled successfully (or the connector already ran)
#   retrying — the start failed inside the failure budget; the loop retries
#   failed   — the budget is spent; terminal until an explicit start
#   deferred — desired but not dispositioned this round (admission held it,
#              e.g. a lifecycle effect owns the resource)
#
# The set is documentation, not a gate: a report whose verdict is not listed
# here still validates.  The vocabulary grows with reporters, not with daemon
# releases, so the daemon records and forwards unknown verdicts rather than
# crashing on a reporter from a newer version.
READINESS_VERDICTS: frozenset[str] = frozenset(
    {"ready", "retrying", "failed", "deferred"}
)


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """One connector disposition: this target was settled this round.

    ``source`` is the reporter identity — the connector key
    (``harness:name``) the daemon's readiness projection aggregates by.
    """

    phase: str
    verdict: str
    source: str

    @classmethod
    def from_payload(cls, payload: Any) -> "ReadinessReport":
        """Validate the minimal schema: all three fields, all strings.

        Missing or wrongly-typed fields are rejected; an unknown verdict
        *value* is not — see ``READINESS_VERDICTS``.
        """

        if not isinstance(payload, dict):
            raise ValueError("readiness report must be a mapping")
        fields: dict[str, str] = {}
        for name in ("phase", "verdict", "source"):
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"readiness report {name!r} must be a non-empty string"
                )
            fields[name] = value
        return cls(**fields)

    def to_payload(self) -> dict[str, str]:
        return {"phase": self.phase, "verdict": self.verdict, "source": self.source}


__all__ = ["READINESS_PHASES", "READINESS_VERDICTS", "ReadinessReport"]
