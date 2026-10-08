"""Routine registry effect records, output union and active-cycle carrier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from hyprial.kernel import PortCommandRejected
from hyprial.biz.impl.routine.contracts.ports import (
    RoutineMutationCompleted,
    RoutineMutationRejected,
    RoutineTimerCompleted,
    RoutinesRecovered,
)
from hyprial.biz.impl.routine.contracts.schema import RoutineSpec

DEFAULT_TASK_TIMEOUT_SECONDS = 24 * 60 * 60.0


@dataclass(frozen=True, slots=True)
class RoutineSourceQueryEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    routine_name: str
    source_kind: str
    source_filter: str
    source_idle_threshold_seconds: float
    coordinator: str
    scheduled_slot_ms: int | None = None
    skip_dispatch: bool = False
    operation: str = "source.query"


@dataclass(frozen=True, slots=True)
class RoutinePacEffect:
    """One PAC call for one task: ``pac.start`` or ``pac.status``."""

    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    routine_name: str
    task_uuid: str
    operation: str
    graph_id: str | None = None
    target: str | None = None
    task_text: str | None = None
    escalate_to: str | None = None
    timeout_seconds: float | None = None
    sender: str | None = None
    role: str = "dispatch"
    occurrence_slot_ms: int | None = None
    rearm_after_created_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class RoutineAlarmEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    routine_name: str
    to: str
    text: str
    idempotency_key: str = ""
    operation: str = "alarm"


RoutineEffect: TypeAlias = (
    RoutineSourceQueryEffect | RoutinePacEffect | RoutineAlarmEffect
)
RegistryOutput: TypeAlias = (
    RoutineEffect
    | RoutineMutationCompleted
    | RoutineMutationRejected
    | RoutinesRecovered
    | RoutineTimerCompleted
    | PortCommandRejected
)


@dataclass(slots=True)
class _ActiveRoutine:
    spec: RoutineSpec
    yaml_text: str
    owner: str
