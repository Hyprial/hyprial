from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from hyprial.contracts.ports import CommandSink, EventSink, PortCommandRejected


@dataclass(frozen=True, slots=True)
class AddRoutineCommand:
    correlation_id: str
    yaml_text: str
    owner: str
    enabled: bool = True


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
    align_schedule: bool = False


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
    quarantine_reason: str | None = None
    registration_id: str | None = None
    role: str = "dispatch"
    mode: str = "source"
    actor: str | None = None
    launch: dict | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "enabled": self.enabled,
            "nextDueMs": self.next_due_ms,
            "sourceErrorStreak": self.source_error_streak,
            "outcomes": list(self.outcomes),
            "registrationId": self.registration_id,
            "mode": self.mode,
            "role": self.role,
            "actor": self.actor or self.produces or self.owner,
            "actorOwnership": "borrowed" if self.actor else ("routine" if self.produces else "external"),
            "launch": self.launch,
            "overlap": "skip" if self.mode == "scheduled" else None,
            "missedPeriods": "skip" if self.mode == "scheduled" else None,
            "inFlight": [item.to_payload() for item in self.in_flight],
            **({"produces": self.produces} if self.produces is not None else {}),
            **(
                {"readable": False, "schemaError": self.schema_error}
                if self.schema_error is not None
                else {}
            ),
            **(
                {"quarantined": True, "quarantineReason": self.quarantine_reason}
                if self.quarantine_reason is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class AddressMigrationProjection:
    routine: str
    field: str
    before: str
    after: str
    migrated_at_ms: int

    def to_payload(self) -> dict[str, object]:
        return {
            "routine": self.routine,
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "migratedAtMs": self.migrated_at_ms,
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
class RoutinePacIoCompleted:
    """One PAC dispatch/projection call settled (U3: no workflow run)."""

    correlation_id: str
    generation: int
    version: int
    routine_name: str
    task_uuid: str
    operation: str
    #: The task's graph id.  ``RoutineInFlightProjection.run_id`` keeps its
    #: published name and now carries this value: the field is part of the
    #: ``routine.status`` payload, so renaming it is a contract change and
    #: belongs with the rest of the retirement (U7), not here.
    graph_id: str | None = None
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
    | RoutinePacIoCompleted
    | RoutineTimerCompleted
    | PortCommandRejected
)
RoutineCommandSink: TypeAlias = CommandSink[RoutineCommand]
RoutineEventSink: TypeAlias = EventSink[RoutineEvent]


class RoutineProjectionPort(Protocol):
    def read_routine(self, name: str) -> RoutineProjection | None: ...

    def read_routines(self) -> tuple[RoutineProjection, ...]: ...
