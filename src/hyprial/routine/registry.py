"""Actor-owned routine registry and durable effect coordinator.

Only :class:`RoutineRegistry` mutates routine state. Taskwarrior, PAC and
alarm calls are represented as outbox effects and run outside the actor.

U3 (retirement of the old dispatcher): a routine task is dispatched as one
PAC graph, not a workflow run.  See
``notes/pac/u3-routine-to-pac-mapping-2026-09-17.md`` and
:mod:`hyprial.routine.pac_dispatch` for the node shape; the rulings this
follows are completion-by-flag, no automatic retry, fixed clock deadlines,
and "report at the deadline, then a human decides".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, TypeAlias
from uuid import uuid4

import yaml

from hyprial.contracts.ports import PortCommandRejected
from hyprial.log import Logger
from hyprial.uri import delivery_address_error

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
    RoutinePacIoCompleted,
)
from .schema import RoutineSchemaError, RoutineSpec, load_routine_text
from .source import SOURCE_ERROR_CAP, SourceTask, route_decision
from .store import (
    AddressMigrationRow,
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
    RoutineSourceQueryEffect | RoutinePacEffect | RoutineAlarmEffect
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
        migrate_address: Callable[[str], str | None] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._generation = generation
        self._publish = publish
        self._clock_ms = clock_ms
        # Resolve one bare agent name to its canonical local URI, or None
        # when the name is not uniquely known on this node.  Used ONLY for
        # stored-spec migration; new submissions are rejected at schema load.
        self._migrate_address = migrate_address
        self._logger = logger
        self._epoch = store.max_version()
        self._last_timer_sequence = 0
        self._active: dict[str, _ActiveRoutine] = {}
        for row in store.list_routines():
            spec: RoutineSpec | None = None
            error: RoutineSchemaError | None = None
            try:
                spec = load_routine_text(row.yaml_text, label=f"stored routine {row.name}")
            except RoutineSchemaError as first_error:
                migrated = self._migrate_stored_addresses(row)
                if migrated is not None:
                    try:
                        spec = load_routine_text(migrated, label=f"stored routine {row.name}")
                        row = replace(row, yaml_text=migrated)
                    except RoutineSchemaError as second_error:
                        error = second_error
                else:
                    error = first_error
            if spec is None:
                assert error is not None
                self._quarantine(row, str(error))
                continue
            if row.quarantine_reason is not None:
                # A spec that validates again (post-migration rewrite) leaves
                # quarantine loudly -- never silently.
                self._store.set_quarantine(row.name, None)
                self._log("info", "routine.unquarantined", routine=row.name)
            self._active[row.name] = _ActiveRoutine(spec, row.yaml_text, row.owner)
            if spec.mode == "scheduled" and row.next_due_ms <= self._clock_ms():
                at = self._clock_ms()
                interval_ms = int(spec.interval_seconds * 1000)
                missed = (at - row.next_due_ms) // interval_ms + 1
                self._store.apply(
                    routine=replace(row, next_due_ms=row.next_due_ms + missed * interval_ms),
                    schedule_events=((row.name, row.next_due_ms, "skipped", missed, "daemon-downtime", at),),
                )
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
        elif isinstance(command, RoutinePacIoCompleted):
            self._pac_completed(command)
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
            enabled=command.enabled,
            next_due_ms=now + int(spec.interval_seconds * 1000),
            source_error_streak=0,
            outcomes="[]",
            created_at_ms=now,
            version=self._next_version(),
            registration_id=uuid4().hex,
        )
        self._store.apply(routine=row, clear_work_for=spec.name)
        self._active[spec.name] = _ActiveRoutine(spec, command.yaml_text, command.owner)
        self._publish(
            RoutineMutationCompleted(
                command.correlation_id,
                self._generation,
                row.version,
                RoutineAddedProjection(
                    spec.name, command.enabled, spec.interval_seconds, row.next_due_ms
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
        if row.quarantine_reason is not None:
            # Quarantine is not pause: enabling a quarantined routine must
            # fail loud with the schema fault, not return it to scheduling.
            self._reject(
                command.correlation_id,
                "ROUTINE_QUARANTINED",
                f"routine {command.name} is quarantined: {row.quarantine_reason}; "
                "fix the spec and re-add the routine",
            )
            return
        active = self._active[row.name]
        next_due = self._clock_ms() + 1000
        if active.spec.mode == "scheduled" or command.align_schedule:
            interval = int(active.spec.interval_seconds * 1000)
            next_due = row.created_at_ms + ((self._clock_ms() - row.created_at_ms) // interval + 1) * interval
        updated = replace(
            row,
            enabled=True,
            next_due_ms=next_due,
            source_error_streak=0,
            outcomes="[]",
            version=self._next_version(),
        )
        self._store.apply(routine=updated, clear_work_for=row.name)
        self._log("info", "routine.resumed", routine=row.name)
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
            if (
                not row.enabled
                or row.quarantine_reason is not None
                or command.observed_at_ms < row.next_due_ms
            ):
                continue
            active = self._active.get(row.name)
            if active is None:
                continue
            interval_ms = int(active.spec.interval_seconds * 1000)
            slot = None
            skip_dispatch = False
            schedule_events = []
            next_due = command.observed_at_ms + interval_ms
            if active.spec.mode == "scheduled":
                missed = (command.observed_at_ms - row.next_due_ms) // interval_ms
                if missed:
                    schedule_events.append((row.name, row.next_due_ms, "skipped", missed,
                                            "missed-period", command.observed_at_ms))
                slot = row.next_due_ms + missed * interval_ms
                next_due = slot + interval_ms
                # Status effects settle earlier work before deciding whether this slot is busy.
                skip_dispatch = False
                if self._store.cycle_for_routine(row.name) is not None:
                    skip_dispatch = True
                    schedule_events.append((row.name, slot, "skipped", 1, "cycle-busy", command.observed_at_ms))
                    self._store.apply(routine=replace(row, next_due_ms=next_due, version=self._next_version()),
                                      schedule_events=tuple(schedule_events))
                    continue
                schedule_events.append((row.name, slot, "admitted", 1, "scheduled", command.observed_at_ms))
            elif self._store.cycle_for_routine(row.name) is not None:
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
                coordinator=active.spec.actor or active.spec.produces or active.owner,
                scheduled_slot_ms=slot, skip_dispatch=skip_dispatch,
            )
            updated = replace(
                row,
                next_due_ms=next_due,
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
                schedule_events=tuple(schedule_events),
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
            RoutinePacEffect(
                effect_id=f"routine-io-{uuid4().hex}",
                parent_correlation_id=cycle.correlation_id,
                generation=self._generation,
                version=cycle.version,
                routine_name=row.name,
                task_uuid=item.task_uuid,
                operation="pac.status",
                graph_id=item.run_id,
                target=item.target,
                # The graph's creator: closing a settled graph authorizes
                # against created_by, so the projection call carries it.
                sender=active.owner,
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

    def _pac_completed(self, event: RoutinePacIoCompleted) -> None:
        effect_row = self._store.effect(event.correlation_id)
        if effect_row is None:
            return
        effect = self.decode_effect(effect_row.payload)
        if isinstance(effect, RoutineAlarmEffect):
            self._store.apply(delete_effects=(effect.effect_id,))
            return
        if not isinstance(effect, RoutinePacEffect):
            return
        row, active, cycle = self._valid_completion(effect)
        if row is None or active is None or cycle is None:
            self._store.apply(delete_effects=(effect.effect_id,))
            return
        if effect.operation == "pac.status":
            self._status_completed(row, active, cycle, effect, event)
        elif effect.operation == "pac.start":
            self._start_completed(row, active, cycle, effect, event)

    def _status_completed(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        cycle: RoutineCycleRow,
        effect: RoutinePacEffect,
        event: RoutinePacIoCompleted,
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
        effects: list[RoutinePacEffect | RoutineAlarmEffect] = []
        pending_start = 0
        for task in tasks:
            if task.uuid in reserved:
                continue
            kind, value = route_decision(
                task.tags, active.spec.routes, active.spec.default_route
            )
            if kind == "escalate":
                escalate_to = value or active.spec.escalate_to
                self._log(
                    "warn",
                    "routine.escalated",
                    routine=row.name,
                    to=escalate_to,
                    task=task.uuid,
                )
                effects.append(
                    RoutineAlarmEffect(
                        effect_id=f"routine-io-{uuid4().hex}",
                        parent_correlation_id=cycle.correlation_id,
                        generation=self._generation,
                        version=cycle.version,
                        routine_name=row.name,
                        to=escalate_to,
                        text=self._render(active.spec, task, nonce=""),
                    )
                )
                outcomes.append("escalated")
                continue
            if slots <= 0:
                if active.spec.mode == "scheduled":
                    slot = int(task.uuid.split(":", 1)[1])
                    self._store.apply(schedule_events=((row.name, slot, "skipped", 1, "previous-occurrence-active", self._clock_ms()),))
                continue
            target = (active.spec.actor or active.spec.produces or active.owner) if kind == "self" else str(value)
            effect_id = f"routine-io-{uuid4().hex}"
            effects.append(
                RoutinePacEffect(
                    effect_id=effect_id,
                    parent_correlation_id=cycle.correlation_id,
                    generation=self._generation,
                    version=cycle.version,
                    routine_name=row.name,
                    task_uuid=task.uuid,
                    operation="pac.start",
                    target=target,
                    # The nonce is gone with the text matcher: completion is
                    # the owner flagging the node (ruling 1, 2026-09-13).
                    task_text=self._render(active.spec, task, nonce=""),
                    role=active.spec.role,
                    escalate_to=active.spec.escalate_to,
                    timeout_seconds=DEFAULT_TASK_TIMEOUT_SECONDS,
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
        effect: RoutinePacEffect,
        event: RoutinePacIoCompleted,
    ) -> None:
        outcomes = list(json.loads(cycle.outcomes_json))
        puts: tuple[InFlightRow, ...] = ()
        if (
            event.code is None
            and event.graph_id is not None
            and effect.target is not None
        ):
            if event.state not in {"done", "escalated"}:
                puts = (
                    InFlightRow(
                        row.name,
                        effect.task_uuid,
                        event.graph_id,
                        effect.target,
                        self._clock_ms(),
                    ),
                )
            # Settled under its durable key: the graph reached done/escalated
            # in an EARLIER cycle and was counted there.  Tracking it again
            # would re-report the same settlement every cycle the source
            # still lists the task, which inflates the breaker's window until
            # it pauses a routine over one finished task.  Allen, 2026-09-17:
            # the task is marked failed and 「由被上报的人/agent决定是否重排」
            # -- so the routine neither re-dispatches it nor re-counts it.
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
        extra_effects: tuple[RoutinePacEffect | RoutineAlarmEffect, ...] = (),
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

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log(level, event, **fields)  # type: ignore[arg-type]
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging must never break the state authority
            pass

    def _quarantine(self, row: RoutineRow, reason: str) -> None:
        """A stored routine that fails validation stops scheduling, loudly.

        The pre-quarantine code swallowed RoutineSchemaError at load and
        silently dropped the routine from the active set (the 2026-09-14
        lark-flycheck class of loss: nobody scheduled it, nobody was told).
        Quarantine is recorded, logged on every startup, blocks resume, and
        is visible in list/doctor projections.
        """

        if row.quarantine_reason != reason:
            self._store.set_quarantine(row.name, reason)
        self._log("error", "routine.quarantined", routine=row.name, error=reason)

    def _migrate_stored_addresses(self, row: RoutineRow) -> str | None:
        """Rewrite bare-name escalate_to addresses in one stored spec.

        All-or-nothing per routine: every undeliverable address must resolve
        uniquely via ``migrate_address`` (this node's agents registry) or
        nothing is rewritten and the caller quarantines -- never guess, never
        half-migrate.  Each rewrite is ledgered with before/after, so a
        repeated startup is idempotent and the change is auditable.
        """

        if self._migrate_address is None:
            return None
        try:
            document = yaml.safe_load(row.yaml_text)
        except yaml.YAMLError:
            return None
        if not isinstance(document, dict):
            return None
        rewrites: list[tuple[str, str, str]] = []
        policy = document.get("policy")
        if isinstance(policy, dict):
            routes = policy.get("routes")
            if isinstance(routes, list):
                for index, item in enumerate(routes):
                    if not isinstance(item, dict):
                        continue
                    value = item.get("escalate_to")
                    if isinstance(value, str) and delivery_address_error(value) is not None:
                        resolved = self._migrate_address(value.strip())
                        if resolved is None:
                            return None
                        rewrites.append(
                            (f"policy.routes[{index}].escalate_to", value.strip(), resolved)
                        )
                        item["escalate_to"] = resolved
        timeout = document.get("on_task_timeout")
        if isinstance(timeout, dict):
            value = timeout.get("escalate_to")
            if isinstance(value, str) and delivery_address_error(value) is not None:
                resolved = self._migrate_address(value.strip())
                if resolved is None:
                    return None
                rewrites.append(("on_task_timeout.escalate_to", value.strip(), resolved))
                timeout["escalate_to"] = resolved
        if not rewrites:
            return None
        new_text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        now = self._clock_ms()
        for field, before, after in rewrites:
            self._store.record_address_migration(
                AddressMigrationRow(row.name, field, before, after, now)
            )
            self._log(
                "info",
                "routine.address_migrated",
                routine=row.name,
                field=field,
                before=before,
                after=after,
                resolvedVia="local-agents-roster",
            )
        self._store.rewrite_yaml(row.name, new_text)
        return new_text

    def _pause_alarm(
        self,
        row: RoutineRow,
        active: _ActiveRoutine,
        correlation_id: str,
        reason: str,
    ) -> RoutineAlarmEffect:
        self._log("error", "routine.paused", routine=row.name, reason=reason)
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
            "RoutinePacEffect": RoutinePacEffect,
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
    def _render(spec: RoutineSpec, task: SourceTask, *, nonce: str) -> str:
        text = spec.task_template
        text = text.replace("{{task.uuid}}", task.uuid)
        text = text.replace("{{task.description}}", task.description)
        text = text.replace("{{task.tags}}", " ".join(task.tags))
        text = text.replace("{{reason}}", task.description)
        # ``nonce`` is always "" since U3 retired the reply matcher: the
        # placeholder is still ACCEPTED by the schema so that a routine.yaml
        # already registered in production does not become a schema_error on
        # upgrade, and it renders to nothing.  The guard's one remaining job
        # is the caller that passes the placeholder itself through unchanged.
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
    "RoutinePacEffect",
]
