"""Actor-owned routine registry and durable effect coordinator.

Only :class:`RoutineRegistry` mutates routine state. Taskwarrior, Workflow and
alarm calls are represented as outbox effects and run outside the actor.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, TypeAlias
from uuid import uuid4

from hyprial.contracts.ports import PortCommandRejected

from .ports import (
    AddRoutineCommand,
    PauseRoutineCommand,
    RecoverRoutinesCommand,
    RemoveRoutineCommand,
    ResumeRoutineCommand,
    RoutineAddedProjection,
    RoutineMutationCompleted,
    RoutineMutationProjection,
    RoutinesRecovered,
    RoutineSourceQueryCompleted,
    RoutineTimerCompleted,
    RoutineTimerElapsedCommand,
    RoutineWorkflowIoCompleted,
)
from .schema import RoutineSchemaError, RoutineSpec, load_routine_text
from .source import SOURCE_ERROR_CAP, SourceTask, route_decision
from .store import (
    InFlightRow,
    RoutineCycleRow,
    RoutineEffectRow,
    RoutineRow,
    RoutineStore,
)


DEFAULT_TASK_TIMEOUT_SECONDS = 3600.0


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
    operation: str = "source.query"


@dataclass(frozen=True, slots=True)
class RoutineWorkflowEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    routine_name: str
    task_uuid: str
    operation: str
    run_id: str | None = None
    target: str | None = None
    workflow_name: str | None = None
    yaml_text: str | None = None
    sender: str | None = None


@dataclass(frozen=True, slots=True)
class RoutineAlarmEffect:
    effect_id: str
    parent_correlation_id: str
    generation: int
    version: int
    routine_name: str
    to: str
    text: str
    operation: str = "alarm"


RoutineEffect: TypeAlias = (
    RoutineSourceQueryEffect | RoutineWorkflowEffect | RoutineAlarmEffect
)
RegistryOutput: TypeAlias = (
    RoutineEffect
    | RoutineMutationCompleted
    | RoutinesRecovered
    | RoutineTimerCompleted
    | PortCommandRejected
)


@dataclass(slots=True)
class _ActiveRoutine:
    spec: RoutineSpec
    yaml_text: str
    owner: str


class RoutineRegistry:
    """Serial handler for schedule, breaker, in-flight and Store mutation."""

    def __init__(
        self,
        *,
        store: RoutineStore,
        generation: int,
        publish: Callable[[RegistryOutput], None],
        clock_ms: Callable[[], int],
    ) -> None:
        self._store = store
        self._generation = generation
        self._publish = publish
        self._clock_ms = clock_ms
        self._epoch = store.max_version()
        self._last_timer_sequence = 0
        self._active: dict[str, _ActiveRoutine] = {}
        for row in store.list_routines():
            try:
                spec = load_routine_text(row.yaml_text, label=f"stored routine {row.name}")
            except RoutineSchemaError:
                continue
            self._active[row.name] = _ActiveRoutine(spec, row.yaml_text, row.owner)
        store.rebase_pending_generation(generation)
        for row in store.pending_effects(limit=10_000):
            self._publish(self.decode_effect(row.payload))

    def __call__(self, command: object) -> None:
        if isinstance(command, AddRoutineCommand):
            self._add(command)
        elif isinstance(command, RemoveRoutineCommand):
            self._remove(command)
        elif isinstance(command, PauseRoutineCommand):
            self._pause(command)
        elif isinstance(command, ResumeRoutineCommand):
            self._resume(command)
        elif isinstance(command, RecoverRoutinesCommand):
            self._recover(command)
        elif isinstance(command, RoutineTimerElapsedCommand):
            self._timer(command)
        elif isinstance(command, RoutineSourceQueryCompleted):
            self._source_completed(command)
        elif isinstance(command, RoutineWorkflowIoCompleted):
            self._workflow_completed(command)
        else:
            raise TypeError(f"unsupported routine command: {type(command).__name__}")

    def _add(self, command: AddRoutineCommand) -> None:
        try:
            spec = load_routine_text(command.yaml_text)
        except RoutineSchemaError as error:
            self._reject(command.correlation_id, "ROUTINE_SCHEMA_ERROR", str(error))
            return
        # Admission is serialized by this actor; never turn add into an upsert.
        if self._store.snapshot(spec.name) is not None:
            self._reject(command.correlation_id, "ROUTINE_EXISTS", f"routine already exists: {spec.name}")
            return
        now = self._clock_ms()
        row = RoutineRow(
            name=spec.name,
            yaml_text=command.yaml_text,
            owner=command.owner,
            enabled=True,
            next_due_ms=now + int(spec.interval_seconds * 1000),
            source_error_streak=0,
            outcomes="[]",
            created_at_ms=now,
            version=self._next_version(),
        )
        self._store.apply(routine=row, clear_work_for=spec.name)
        self._active[spec.name] = _ActiveRoutine(spec, command.yaml_text, command.owner)
        self._publish(
            RoutineMutationCompleted(
                command.correlation_id,
                self._generation,
                row.version,
                RoutineAddedProjection(
                    spec.name, True, spec.interval_seconds, row.next_due_ms
                ),
            )
        )

    def _remove(self, command: RemoveRoutineCommand) -> None:
        if self._store.get_routine(command.name) is None:
            self._reject(
                command.correlation_id,
                "ROUTINE_NOT_FOUND",
                f"no such routine: {command.name}",
            )
            return
        version = self._next_version()
        self._store.apply(remove_routine=command.name)
        self._active.pop(command.name, None)
        self._publish(
            RoutineMutationCompleted(
                command.correlation_id,
                self._generation,
                version,
                RoutineMutationProjection(command.name, removed=True),
            )
        )

    def _pause(self, command: PauseRoutineCommand) -> None:
        row = self._require(command.correlation_id, command.name)
        if row is None:
            return
        updated = replace(row, enabled=False, version=self._next_version())
        # Accepted external work is fenced and removed from the outbox. A
        # completion already running outside the actor is harmlessly stale.
        self._store.apply(routine=updated, clear_work_for=row.name)
        self._publish_mutation(command.correlation_id, updated, enabled=False)

    def _resume(self, command: ResumeRoutineCommand) -> None:
        row = self._require(command.correlation_id, command.name)
        if row is None:
            return
        updated = replace(
            row,
            enabled=True,
            next_due_ms=self._clock_ms() + 1000,
            source_error_streak=0,
            outcomes="[]",
            version=self._next_version(),
        )
        self._store.apply(routine=updated, clear_work_for=row.name)
        self._publish_mutation(command.correlation_id, updated, enabled=True)

    def _recover(self, command: RecoverRoutinesCommand) -> None:
        self._publish(
            RoutinesRecovered(
                command.correlation_id,
                self._generation,
                self._epoch,
                len(self._active),
            )
        )

    def _timer(self, command: RoutineTimerElapsedCommand) -> None:
        # command.version is a timer sequence, not a Store version.
        if (
            command.generation != self._generation
            or command.version <= self._last_timer_sequence
        ):
            self._reject(
                command.correlation_id,
                "ROUTINE_STALE_TIMER",
                f"timer {command.generation}/{command.version} is stale for "
                f"{self._generation}/{self._last_timer_sequence}",
            )
            return
        self._last_timer_sequence = command.version
        checked = 0
        for row in self._store.list_routines():
            if not row.enabled or command.observed_at_ms < row.next_due_ms:
                continue
            active = self._active.get(row.name)
            if active is None or self._store.cycle_for_routine(row.name) is not None:
                continue
            checked += 1
            version = self._next_version()
            correlation_id = f"routine-cycle-{uuid4().hex}"
            effect = RoutineSourceQueryEffect(
                effect_id=f"routine-io-{uuid4().hex}",
                parent_correlation_id=correlation_id,
                generation=self._generation,
                version=version,
                routine_name=row.name,
                source_kind=active.spec.source_kind,
                source_filter=active.spec.source_filter,
                source_idle_threshold_seconds=active.spec.source_idle_threshold_seconds,
                coordinator=active.spec.produces or active.owner,
            )
            updated = replace(
                row,
                next_due_ms=command.observed_at_ms
                + int(active.spec.interval_seconds * 1000),
                version=version,
            )
            cycle = RoutineCycleRow(
                correlation_id=correlation_id,
                routine_name=row.name,
                generation=self._generation,
                version=version,
                outcomes_json=row.outcomes,
            )
            self._store.apply(
                routine=updated,
                cycle=cycle,
                add_effects=(self.effect_row(effect),),
            )
            self._publish(effect)
        self._publish(
            RoutineTimerCompleted(
                command.correlation_id,
                self._generation,
                self._epoch,
                checked,
            )
        )

    def _source_completed(self, event: RoutineSourceQueryCompleted) -> None:
        effect_row = self._store.effect(event.correlation_id)
        if effect_row is None:
            return
        effect = self.decode_effect(effect_row.payload)
        if not isinstance(effect, RoutineSourceQueryEffect):
            return
        row, active, cycle = self._valid_completion(effect)
        if row is None or active is None or cycle is None:
            self._store.apply(delete_effects=(effect.effect_id,))
            return
        if event.code is not None:
            self._source_failed(row, active, cycle, effect, event)
            return
        tasks = tuple(
            SourceTask(item.uuid, item.description, item.tags) for item in event.tasks
        )
        status_effects = tuple(
            RoutineWorkflowEffect(
                effect_id=f"routine-io-{uuid4().hex}",
                parent_correlation_id=cycle.correlation_id,
                generation=self._generation,
                version=cycle.version,
                routine_name=row.name,
                task_uuid=item.task_uuid,
                operation="workflow.status",
                run_id=item.run_id,
                target=item.target,
            )
            for item in self._store.in_flight(routine=row.name)
        )
        cycle = replace(
            cycle,
            tasks_json=self._encode_tasks(tasks),
            pending_status=len(status_effects),
        )
        if status_effects:
            self._store.apply(
                cycle=cycle,
                add_effects=tuple(self.effect_row(item) for item in status_effects),
                delete_effects=(effect.effect_id,),
            )
            for item in status_effects:
                self._publish(item)
            return
        self._plan_starts(
            row,
            active,
            cycle,
            tasks,
            delete_effects=(effect.effect_id,),
        )

    def _source_failed(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        effect: RoutineSourceQueryEffect,
        event: RoutineSourceQueryCompleted,
    ) -> None:
        streak = row.source_error_streak + 1
        pause = event.permanent or streak >= SOURCE_ERROR_CAP
        updated = replace(
            row,
            enabled=not pause,
            source_error_streak=streak,
            version=self._next_version(),
        )
        alarms: tuple[RoutineAlarmEffect, ...] = ()
        if pause:
            reason = (
                f"permanent source error (never retried): {event.detail or event.code}"
                if event.permanent
                else f"source failed {streak} consecutive cycles: {event.detail or event.code}"
            )
            alarms = (self._pause_alarm(updated, active, cycle.correlation_id, reason),)
        self._store.apply(
            routine=updated,
            remove_cycle=cycle.correlation_id,
            add_effects=tuple(self.effect_row(item) for item in alarms),
            delete_effects=(effect.effect_id,),
        )
        for alarm in alarms:
            self._publish(alarm)

    def _workflow_completed(self, event: RoutineWorkflowIoCompleted) -> None:
        effect_row = self._store.effect(event.correlation_id)
        if effect_row is None:
            return
        effect = self.decode_effect(effect_row.payload)
        if isinstance(effect, RoutineAlarmEffect):
            self._store.apply(delete_effects=(effect.effect_id,))
            return
        if not isinstance(effect, RoutineWorkflowEffect):
            return
        row, active, cycle = self._valid_completion(effect)
        if row is None or active is None or cycle is None:
            self._store.apply(delete_effects=(effect.effect_id,))
            return
        if effect.operation == "workflow.status":
            self._status_completed(row, active, cycle, effect, event)
        elif effect.operation == "workflow.start":
            self._start_completed(row, active, cycle, effect, event)

    def _status_completed(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        effect: RoutineWorkflowEffect,
        event: RoutineWorkflowIoCompleted,
    ) -> None:
        outcomes = list(json.loads(cycle.outcomes_json))
        removals: tuple[tuple[str, str], ...] = ()
        if event.code is not None or event.state != "running":
            outcomes.append(
                "escalated"
                if event.code is not None or event.state in {None, "escalated"}
                else "done"
            )
            removals = ((row.name, effect.task_uuid),)
        remaining = max(0, cycle.pending_status - 1)
        cycle = replace(
            cycle,
            outcomes_json=json.dumps(outcomes),
            pending_status=remaining,
        )
        if remaining:
            self._store.apply(
                cycle=cycle,
                remove_in_flight=removals,
                delete_effects=(effect.effect_id,),
            )
            return
        self._plan_starts(
            row,
            active,
            cycle,
            self._decode_tasks(cycle.tasks_json),
            remove_in_flight=removals,
            delete_effects=(effect.effect_id,),
        )

    def _plan_starts(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        tasks: tuple[SourceTask, ...],
        *,
        remove_in_flight: tuple[tuple[str, str], ...] = (),
        delete_effects: tuple[str, ...] = (),
    ) -> None:
        outcomes = list(json.loads(cycle.outcomes_json))
        in_flight = {
            item.task_uuid: item for item in self._store.in_flight(routine=row.name)
        }
        for _, task_uuid in remove_in_flight:
            in_flight.pop(task_uuid, None)
        reserved = set(in_flight)
        slots = active.spec.limits.max_in_flight - len(in_flight)
        effects: list[RoutineWorkflowEffect | RoutineAlarmEffect] = []
        pending_start = 0
        for task in tasks:
            if task.uuid in reserved:
                continue
            kind, value = route_decision(
                task.tags, active.spec.routes, active.spec.default_route
            )
            if kind == "escalate":
                effects.append(
                    RoutineAlarmEffect(
                        effect_id=f"routine-io-{uuid4().hex}",
                        parent_correlation_id=cycle.correlation_id,
                        generation=self._generation,
                        version=cycle.version,
                        routine_name=row.name,
                        to=value or active.spec.escalate_to,
                        text=self._render(active.spec, task, nonce=""),
                    )
                )
                outcomes.append("escalated")
                continue
            if slots <= 0:
                continue
            target = active.owner if kind == "self" else str(value)
            effect_id = f"routine-io-{uuid4().hex}"
            workflow_name = self._workflow_name(active.spec, task, effect_id)
            effects.append(
                RoutineWorkflowEffect(
                    effect_id=effect_id,
                    parent_correlation_id=cycle.correlation_id,
                    generation=self._generation,
                    version=cycle.version,
                    routine_name=row.name,
                    task_uuid=task.uuid,
                    operation="workflow.start",
                    target=target,
                    workflow_name=workflow_name,
                    yaml_text=self._workflow_yaml_for(
                        active.spec, active, task, target, workflow_name
                    ),
                    sender=active.owner,
                )
            )
            slots -= 1
            reserved.add(task.uuid)
            pending_start += 1
        cycle = replace(
            cycle,
            tasks_json=self._encode_tasks(tasks),
            outcomes_json=json.dumps(outcomes),
            pending_status=0,
            pending_start=pending_start,
        )
        if pending_start:
            self._store.apply(
                cycle=cycle,
                remove_in_flight=remove_in_flight,
                add_effects=tuple(self.effect_row(item) for item in effects),
                delete_effects=delete_effects,
            )
        else:
            self._finish_cycle(
                row,
                active,
                cycle,
                extra_effects=tuple(effects),
                remove_in_flight=remove_in_flight,
                delete_effects=delete_effects,
            )
        for item in effects:
            self._publish(item)

    def _start_completed(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        effect: RoutineWorkflowEffect,
        event: RoutineWorkflowIoCompleted,
    ) -> None:
        outcomes = list(json.loads(cycle.outcomes_json))
        puts: tuple[InFlightRow, ...] = ()
        if event.code is None and event.run_id is not None and effect.target is not None:
            puts = (
                InFlightRow(
                    row.name,
                    effect.task_uuid,
                    event.run_id,
                    effect.target,
                    self._clock_ms(),
                ),
            )
        else:
            outcomes.append("escalated")
        remaining = max(0, cycle.pending_start - 1)
        cycle = replace(
            cycle,
            outcomes_json=json.dumps(outcomes),
            pending_start=remaining,
        )
        if remaining:
            self._store.apply(
                cycle=cycle,
                put_in_flight=puts,
                delete_effects=(effect.effect_id,),
            )
            return
        self._finish_cycle(
            row,
            active,
            cycle,
            put_in_flight=puts,
            delete_effects=(effect.effect_id,),
        )

    def _finish_cycle(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        *,
        extra_effects: tuple[RoutineWorkflowEffect | RoutineAlarmEffect, ...] = (),
        put_in_flight: tuple[InFlightRow, ...] = (),
        remove_in_flight: tuple[tuple[str, str], ...] = (),
        delete_effects: tuple[str, ...] = (),
    ) -> None:
        outcomes = list(json.loads(cycle.outcomes_json))
        window = active.spec.limits.circuit_breaker.window_runs
        outcomes = outcomes[-window:]
        pause = (
            len(outcomes) >= window
            and sum(item == "escalated" for item in outcomes) / len(outcomes)
            >= active.spec.limits.circuit_breaker.escalate_ratio
            and active.spec.limits.circuit_breaker.action == "pause+alarm"
        )
        updated = replace(
            row,
            enabled=not pause,
            source_error_streak=0,
            outcomes=json.dumps(outcomes),
            version=self._next_version(),
        )
        effects = list(extra_effects)
        if pause:
            effects.append(
                self._pause_alarm(
                    updated,
                    active,
                    cycle.correlation_id,
                    f"circuit breaker: escalation ratio reached across last {window} outcomes",
                )
            )
        self._store.apply(
            routine=updated,
            remove_cycle=cycle.correlation_id,
            put_in_flight=put_in_flight,
            remove_in_flight=remove_in_flight,
            add_effects=tuple(self.effect_row(item) for item in effects),
            delete_effects=delete_effects,
        )
        for item in effects[len(extra_effects) :]:
            self._publish(item)

    def _valid_completion(
        self, effect: RoutineEffect
    ) -> tuple[RoutineRow | None, _ActiveRoutine | None, RoutineCycleRow | None]:
        row = self._store.get_routine(effect.routine_name)
        active = self._active.get(effect.routine_name)
        cycle = self._store.cycle(effect.parent_correlation_id)
        if (
            row is None
            or active is None
            or cycle is None
            or not row.enabled
            or effect.generation != self._generation
            or cycle.generation != self._generation
            or effect.version != cycle.version
            or row.version != cycle.version
        ):
            return None, None, None
        return row, active, cycle

    def _pause_alarm(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        correlation_id: str,
        reason: str,
    ) -> RoutineAlarmEffect:
        return RoutineAlarmEffect(
            effect_id=f"routine-io-{uuid4().hex}",
            parent_correlation_id=correlation_id,
            generation=self._generation,
            version=row.version,
            routine_name=row.name,
            to=active.spec.escalate_to,
            text=(
                f"self-drive routine '{row.name}' paused: {reason}. Resume with "
                f"`hyprial routine resume {row.name}` after review."
            ),
        )

    def _publish_mutation(
        self, correlation_id: str, row: RoutineRow, *, enabled: bool
    ) -> None:
        self._publish(
            RoutineMutationCompleted(
                correlation_id,
                self._generation,
                row.version,
                RoutineMutationProjection(row.name, enabled=enabled),
            )
        )

    def _require(self, correlation_id: str, name: str) -> RoutineRow | None:
        row = self._store.get_routine(name)
        if row is None:
            self._reject(correlation_id, "ROUTINE_NOT_FOUND", f"no such routine: {name}")
        return row

    def _reject(self, correlation_id: str, code: str, detail: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="routine",
                generation=self._generation,
                version=self._epoch,
                code=code,
                detail=detail,
            )
        )

    def _next_version(self) -> int:
        self._epoch += 1
        return self._epoch

    @staticmethod
    def effect_row(effect: RoutineEffect) -> RoutineEffectRow:
        return RoutineEffectRow(
            effect.effect_id,
            effect.parent_correlation_id,
            effect.routine_name,
            effect.generation,
            effect.version,
            {"kind": type(effect).__name__, **asdict(effect)},
        )

    @staticmethod
    def decode_effect(payload: dict[str, Any]) -> RoutineEffect:
        values = dict(payload)
        kind = str(values.pop("kind"))
        types = {
            "RoutineSourceQueryEffect": RoutineSourceQueryEffect,
            "RoutineWorkflowEffect": RoutineWorkflowEffect,
            "RoutineAlarmEffect": RoutineAlarmEffect,
        }
        if kind == "RoutineSourceQueryEffect":
            values.setdefault("coordinator", "")
        try:
            return types[kind](**values)
        except KeyError as error:
            raise ValueError(f"unknown routine effect kind: {kind}") from error

    @staticmethod
    def _encode_tasks(tasks: tuple[SourceTask, ...]) -> str:
        return json.dumps(
            [
                {"uuid": item.uuid, "description": item.description, "tags": list(item.tags)}
                for item in tasks
            ],
            sort_keys=True,
        )

    @staticmethod
    def _decode_tasks(payload: str) -> tuple[SourceTask, ...]:
        return tuple(
            SourceTask(
                uuid=str(item["uuid"]),
                description=str(item["description"]),
                tags=tuple(str(tag) for tag in item["tags"]),
            )
            for item in json.loads(payload)
        )

    @staticmethod
    def _workflow_name(spec: RoutineSpec, task: SourceTask, effect_id: str) -> str:
        task_hint = task.uuid.replace("-", "")[:8]
        effect_hint = effect_id.rsplit("-", 1)[-1][:12]
        return f"sd-{spec.name}-{task_hint}-{effect_hint}"

    def _workflow_yaml_for(
        self,
        spec: RoutineSpec,
        active: _ActiveRoutine,
        task: SourceTask,
        target: str,
        workflow_name: str,
    ) -> str:
        task_text = self._render(spec, task, nonce="{{nonce}}")
        return (
            "version: 1\n"
            f"name: {workflow_name}\n"
            f"task: {json.dumps(task_text)}\n"
            f"targets: [{json.dumps(target)}]\n"
            "await:\n"
            "  kind: reply\n"
            f"  timeout: {int(DEFAULT_TASK_TIMEOUT_SECONDS)}s\n"
            '  match: "DONE {{nonce}}"\n'
            "on_timeout:\n"
            "  action: escalate\n"
            f"  escalate_to: {json.dumps(spec.escalate_to)}\n"
            f"report_to: {json.dumps(active.owner)}\n"
        )

    @staticmethod
    def _render(spec: RoutineSpec, task: SourceTask, *, nonce: str) -> str:
        text = spec.task_template
        text = text.replace("{{task.uuid}}", task.uuid)
        text = text.replace("{{task.description}}", task.description)
        text = text.replace("{{task.tags}}", " ".join(task.tags))
        text = text.replace("{{reason}}", task.description)
        if nonce != "{{nonce}}":
            text = text.replace("{{nonce}}", nonce)
        return text


__all__ = [
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "RegistryOutput",
    "RoutineAlarmEffect",
    "RoutineEffect",
    "RoutineRegistry",
    "RoutineSourceQueryEffect",
    "RoutineWorkflowEffect",
]
