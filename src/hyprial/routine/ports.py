from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class AddRoutineCommand:
    correlation_id: str
    yaml_text: str
    owner: str


@dataclass(frozen=True, slots=True)
class RemoveRoutineCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class PauseRoutineCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ResumeRoutineCommand:
    correlation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class RecoverRoutinesCommand:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RoutineTimerElapsedCommand:
    correlation_id: str
    generation: int
    version: int
    observed_at_ms: int


RoutineCommand: TypeAlias = (
    AddRoutineCommand
    | RemoveRoutineCommand
    | PauseRoutineCommand
    | ResumeRoutineCommand
    | RecoverRoutinesCommand
    | RoutineTimerElapsedCommand
)


@dataclass(frozen=True, slots=True)
class RoutineInFlightProjection:
    task_uuid: str
    run_id: str
    target: str

    def to_payload(self) -> dict[str, object]:
        return {"taskUuid": self.task_uuid, "runId": self.run_id, "target": self.target}


@dataclass(frozen=True, slots=True)
class RoutineProjection:
    version: int
    name: str
    owner: str
    enabled: bool
    next_due_ms: int
    source_error_streak: int
    outcomes: tuple[str, ...]
    in_flight: tuple[RoutineInFlightProjection, ...]
    produces: str | None = None
    schema_error: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "enabled": self.enabled,
            "nextDueMs": self.next_due_ms,
            "sourceErrorStreak": self.source_error_streak,
            "outcomes": list(self.outcomes),
            "inFlight": [item.to_payload() for item in self.in_flight],
            **({"produces": self.produces} if self.produces is not None else {}),
            **(
                {"readable": False, "schemaError": self.schema_error}
                if self.schema_error is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class RoutineAddedProjection:
    name: str
    enabled: bool
    interval_seconds: float
    next_due_ms: int

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "intervalSeconds": self.interval_seconds,
            "nextDueMs": self.next_due_ms,
        }


@dataclass(frozen=True, slots=True)
class RoutinesProjection:
    routines: tuple[RoutineProjection, ...]

    def to_payload(self) -> dict[str, object]:
        return {"routines": [routine.to_payload() for routine in self.routines]}


@dataclass(frozen=True, slots=True)
class RoutineMutationProjection:
    name: str
    enabled: bool | None = None
    removed: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            **({"enabled": self.enabled} if self.enabled is not None else {}),
            **({"removed": self.removed} if self.removed is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class RoutineSourceTaskProjection:
    uuid: str
    description: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutineMutationCompleted:
    correlation_id: str
    generation: int
    version: int
    result: RoutineAddedProjection | RoutineMutationProjection


@dataclass(frozen=True, slots=True)
class RoutinesRecovered:
    correlation_id: str
    generation: int
    version: int
    adopted: int


@dataclass(frozen=True, slots=True)
class RoutineSourceQueryCompleted:
    correlation_id: str
    generation: int
    version: int
    routine_name: str
    tasks: tuple[RoutineSourceTaskProjection, ...]
    code: str | None = None
    detail: str | None = None
    permanent: bool = False


@dataclass(frozen=True, slots=True)
class RoutineWorkflowIoCompleted:
    correlation_id: str
    generation: int
    version: int
    routine_name: str
    task_uuid: str
    operation: str
    run_id: str | None = None
    state: str | None = None
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RoutineTimerCompleted:
    correlation_id: str
    generation: int
    version: int
    routines_checked: int


RoutineEvent: TypeAlias = (
    RoutineMutationCompleted
    | RoutinesRecovered
    | RoutineSourceQueryCompleted
    | RoutineWorkflowIoCompleted
    | RoutineTimerCompleted
    | PortCommandRejected
)
RoutineCommandSink: TypeAlias = CommandSink[RoutineCommand]
RoutineEventSink: TypeAlias = EventSink[RoutineEvent]


class RoutineProjectionPort(Protocol):
    def read_routine(self, name: str) -> RoutineProjection | None: ...

    def read_routines(self) -> tuple[RoutineProjection, ...]: ...
