"""Internal incarnation-bound transfer start admission protocol."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class HoldStartsForTransfer:
    correlation_id: str
    actor: str = field(kw_only=True)
    expected_entity_token: str = field(kw_only=True)
    operation_id: str = field(kw_only=True)
    generation: int = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class StartsHeld:
    correlation_id: str
    actor: str
    expected_entity_token: str
    operation_id: str
    generation: int
    disposition: Literal["held", "entity-mismatch", "unavailable"]
    drained: tuple[str, ...] = ()
    # The owning harness runtime's projection version at the moment the hold
    # state was observed. The P2 ``generation`` fences the incarnation; this
    # fences the derived hold document itself, like every other HarnessEvent.
    version: int = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class ReleaseStartsHold:
    """Internal command; caller must durably authorize/audit override first."""

    correlation_id: str
    actor: str = field(kw_only=True)
    expected_entity_token: str = field(kw_only=True)
    operation_id: str = field(kw_only=True)
    generation: int = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class StartsHoldReleased:
    correlation_id: str
    actor: str
    expected_entity_token: str
    operation_id: str
    generation: int
    disposition: Literal["released"] = "released"
    version: int = field(kw_only=True)
