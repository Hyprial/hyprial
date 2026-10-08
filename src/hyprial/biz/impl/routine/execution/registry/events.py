"""RoutineRegistry source/PAC/status completion and cycle bookkeeping."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

import yaml

from hyprial.kernel import PortCommandRejected
from hyprial.kernel import delivery_address_error

from hyprial.biz.impl.routine.contracts.ports import (
    RoutineMutationCompleted,
    RoutineMutationProjection,
    RoutineMutationRejected,
    RoutinePacIoCompleted,
    RoutineSourceQueryCompleted,
)
from hyprial.biz.impl.routine.contracts.schema import RoutineSpec
from hyprial.biz.impl.routine.execution.source import SOURCE_ERROR_CAP, SourceTask, route_decision
from hyprial.biz.impl.routine.storage.store import (
    AddressMigrationRow,
    InFlightRow,
    RoutineCycleRow,
    RoutineEffectRow,
    RoutineRow,
)
from hyprial.biz.impl.routine.execution.registry.effects import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    RoutineAlarmEffect,
    RoutineEffect,
    RoutinePacEffect,
    RoutineSourceQueryEffect,
    _ActiveRoutine,
)

class _RegistryEvents:
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
            if event.code is None or event.permanent:
                self._store.apply(delete_effects=(effect.effect_id,))
            else:
                effect_row = self._store.effect(effect.effect_id)
                attempts = 1 if effect_row is None else effect_row.attempts + 1
                delay_ms = min(60_000, 500 * (2 ** min(attempts - 1, 7)))
                self._store.retry_effect(
                    effect.effect_id,
                    attempts=attempts,
                    available_at_ms=self._clock_ms() + delay_ms,
                )
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
                        idempotency_key=(
                            f"routine-alarm:{row.name}:{cycle.correlation_id}:"
                            f"route:{task.uuid}"
                        ),
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
                    occurrence_slot_ms=(
                        cycle.occurrence_slot_ms
                        if active.spec.mode == "source"
                        else None
                    ),
                    rearm_after_created_at_ms=row.rearm_after_created_at_ms,
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
        alarms: tuple[RoutineAlarmEffect, ...] = ()
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
            escalate_to = effect.escalate_to or active.spec.escalate_to
            code = event.code or "ROUTINE_START_FAILED"
            detail = event.detail or "PAC start returned no graph"
            alarm = RoutineAlarmEffect(
                effect_id=f"routine-io-{uuid4().hex}",
                parent_correlation_id=cycle.correlation_id,
                generation=self._generation,
                version=cycle.version,
                routine_name=row.name,
                to=escalate_to,
                text=(
                    f"routine '{row.name}' failed to start task "
                    f"'{effect.task_uuid}': {code}: {detail}"
                ),
                idempotency_key=(
                    f"routine-start-failed:{row.name}:{cycle.correlation_id}:"
                    f"{effect.task_uuid}"
                ),
            )
            alarms = (alarm,)
            self._log(
                "warn",
                "routine.start_failed",
                routine=row.name,
                task=effect.task_uuid,
                code=code,
                detail=detail,
                to=escalate_to,
            )
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
                add_effects=tuple(self.effect_row(item) for item in alarms),
                delete_effects=(effect.effect_id,),
            )
            for alarm in alarms:
                self._publish(alarm)
            return
        self._finish_cycle(
            row,
            active,
            cycle,
            extra_effects=alarms,
            put_in_flight=puts,
            delete_effects=(effect.effect_id,),
        )
        for alarm in alarms:
            self._publish(alarm)

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
            idempotency_key=f"routine-paused:{row.name}:{correlation_id}",
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

    def _reject(
        self,
        correlation_id: str,
        code: str,
        detail: str,
        *,
        data: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if data:
            self._publish(
                RoutineMutationRejected(
                    correlation_id, self._generation, self._epoch,
                    code, detail, data,
                )
            )
            return
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
            0,
            0,
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
        if kind == "RoutinePacEffect":
            values.setdefault("occurrence_slot_ms", None)
            values.setdefault("rearm_after_created_at_ms", 0)
        if kind == "RoutineAlarmEffect":
            values.setdefault("idempotency_key", str(values.get("effect_id", "")))
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
