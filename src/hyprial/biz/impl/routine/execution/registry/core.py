"""RoutineRegistry command, timer and recovery handling (mixin)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from hyprial.kernel import ipc_errors
from hyprial.kernel import Logger

from hyprial.biz.impl.routine.contracts.ports import (
    AddRoutineCommand,
    CancelRoutineRemovalCommand,
    PauseRoutineCommand,
    RecoverRoutinesCommand,
    RemoveRoutineCommand,
    ReserveRoutineRemovalCommand,
    ResumeRoutineCommand,
    RoutineAddedProjection,
    RoutineMutationCompleted,
    RoutineMutationProjection,
    RoutinePacIoCompleted,
    RoutineSourceQueryCompleted,
    RoutineTimerCompleted,
    RoutineTimerElapsedCommand,
    RoutinesRecovered,
    SetRoutineCommand,
)
from hyprial.biz.impl.routine.contracts.schema import RoutineSchemaError, RoutineSpec, load_routine_text
from hyprial.biz.impl.routine.storage.store import RoutineCycleRow, RoutineRow, RoutineStore
from hyprial.biz.impl.routine.execution.registry.effects import (
    RegistryOutput,
    RoutineSourceQueryEffect,
    _ActiveRoutine,
)

class _RegistryCore:
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
        for row in store.pending_effects(limit=10_000, now_ms=self._clock_ms()):
            self._publish(self.decode_effect(row.payload))

    def __call__(self, command: object) -> None:
        if isinstance(command, AddRoutineCommand):
            self._add(command)
        elif isinstance(command, SetRoutineCommand):
            self._set(command)
        elif isinstance(command, RemoveRoutineCommand):
            self._remove(command)
        elif isinstance(command, ReserveRoutineRemovalCommand):
            self._reserve_remove(command)
        elif isinstance(command, CancelRoutineRemovalCommand):
            self._cancel_remove(command)
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
        if command.registration_id is not None and not command.registration_id.strip():
            self._reject(command.correlation_id, "ROUTINE_REGISTRATION_INVALID", "registration ID is blank")
            return
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
            registration_id=command.registration_id or uuid4().hex,
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
        row = self._store.get_routine(command.name)
        if row is None:
            self._reject(
                command.correlation_id,
                "ROUTINE_NOT_FOUND",
                f"no such routine: {command.name}",
            )
            return
        current = self._active.get(command.name)
        binding = (
            None if current is None else current.spec.actor or current.spec.produces
        )
        reservation = self._store.removal_reservation(command.name)
        if command.reservation_id is not None:
            if (
                reservation is None
                or reservation.reservation_id != command.reservation_id
                or reservation.registration_id != row.registration_id
            ):
                self._reject(
                    command.correlation_id,
                    "ROUTINE_COORDINATOR_CONFLICT",
                    f"routine {command.name!r} removal reservation changed",
                )
                return
        elif reservation is not None:
            self._reject(
                command.correlation_id,
                "ROUTINE_BUSY",
                f"routine {command.name!r} has a reserved removal",
            )
            return
        if command.enforce_last and binding is not None and reservation is None:
            reserved = self._store.reserved_removals()
            bound = sum(
                1
                for name, item in self._active.items()
                if (item.spec.actor or item.spec.produces) == binding
                and name not in reserved
            )
            if bound <= 1:
                self._reject(
                    command.correlation_id,
                    ipc_errors.ROUTINE_LAST_BINDING,
                    f"routine {command.name!r} is the last routine bound to "
                    f"{binding}; bind a new one first with `hyprial routine add`, "
                    "or modify this one with `hyprial routine set`",
                    data=(("routine", command.name), ("actor", binding)),
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

    def _reserve_remove(self, command: ReserveRoutineRemovalCommand) -> None:
        row = self._store.get_routine(command.name)
        if row is None:
            self._reject(
                command.correlation_id,
                "ROUTINE_NOT_FOUND",
                f"no such routine: {command.name}",
            )
            return
        existing = self._store.removal_reservation(command.name)
        if (
            existing is not None
            and existing.reservation_id == command.reservation_id
            and existing.registration_id == row.registration_id
        ):
            self._publish_mutation(command.correlation_id, row, enabled=row.enabled)
            return
        if existing is not None:
            self._reject(
                command.correlation_id,
                "ROUTINE_BUSY",
                f"routine {command.name!r} has a reserved removal",
            )
            return
        current = self._active.get(command.name)
        binding = None if current is None else current.spec.actor or current.spec.produces
        if command.enforce_last and binding is not None:
            reserved = self._store.reserved_removals()
            alternatives = sum(
                1
                for name, item in self._active.items()
                if name != command.name
                and name not in reserved
                and (item.spec.actor or item.spec.produces) == binding
            )
            if alternatives == 0:
                self._reject(
                    command.correlation_id,
                    ipc_errors.ROUTINE_LAST_BINDING,
                    f"routine {command.name!r} is the last unreserved routine "
                    f"bound to {binding}; bind a new one first with "
                    "`hyprial routine add`, or modify this one with "
                    "`hyprial routine set`",
                    data=(("routine", command.name), ("actor", binding)),
                )
                return
        self._store.reserve_removal(
            command.name, command.reservation_id, row.registration_id
        )
        self._publish_mutation(command.correlation_id, row, enabled=row.enabled)

    def _cancel_remove(self, command: CancelRoutineRemovalCommand) -> None:
        self._store.cancel_removal_reservation(
            command.name, command.reservation_id
        )
        row = self._store.get_routine(command.name)
        self._publish(
            RoutineMutationCompleted(
                command.correlation_id,
                self._generation,
                self._next_version(),
                RoutineMutationProjection(
                    command.name,
                    enabled=None if row is None else row.enabled,
                ),
            )
        )

    def _set(self, command: SetRoutineCommand) -> None:
        row = self._require(command.correlation_id, command.name)
        if row is None:
            return
        if self._store.removal_reservation(command.name) is not None:
            self._reject(
                command.correlation_id,
                "ROUTINE_BUSY",
                f"routine {command.name!r} has a reserved removal",
            )
            return
        snapshot = self._store.snapshot(command.name)
        if (snapshot is not None and bool(snapshot.in_flight)) or (
            self._store.cycle_for_routine(command.name) is not None
        ):
            self._reject(
                command.correlation_id,
                "ROUTINE_BUSY",
                f"routine {command.name!r} has an active cycle; replace it after the cycle settles",
            )
            return
        try:
            spec = load_routine_text(command.yaml_text)
        except RoutineSchemaError as error:
            self._reject(command.correlation_id, "ROUTINE_SCHEMA_ERROR", str(error))
            return
        if spec.name != command.name:
            self._reject(
                command.correlation_id,
                ipc_errors.ROUTINE_NAME_IMMUTABLE,
                f"replacement name must remain {command.name!r}",
            )
            return
        current = self._active[command.name].spec
        if (spec.actor, spec.produces) != (current.actor, current.produces):
            self._reject(
                command.correlation_id,
                ipc_errors.ROUTINE_BINDING_IMMUTABLE,
                "routine set keeps the existing actor/produces binding",
            )
            return
        updated = replace(
            row,
            yaml_text=command.yaml_text,
            next_due_ms=self._clock_ms() + int(spec.interval_seconds * 1000),
            version=self._next_version(),
        )
        self._store.apply(routine=updated)
        self._active[command.name] = _ActiveRoutine(
            spec, command.yaml_text, row.owner
        )
        self._publish_mutation(
            command.correlation_id, updated, enabled=updated.enabled
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
            rearm_after_created_at_ms=command.rearm_after_created_at_ms,
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
        reserved = self._store.reserved_removals()
        for row in self._store.list_routines():
            if (
                not row.enabled
                or row.name in reserved
                or row.quarantine_reason is not None
                or command.observed_at_ms < row.next_due_ms
            ):
                continue
            active = self._active.get(row.name)
            if active is None:
                continue
            interval_ms = int(active.spec.interval_seconds * 1000)
            slot = row.next_due_ms
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
                occurrence_slot_ms=slot,
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
