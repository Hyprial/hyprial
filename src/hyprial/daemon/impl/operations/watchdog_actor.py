"""Watchdog authorities with durable notification effects off the tick.

The existing watchdog policies run against in-memory state. A separate effect
lane persists an outbox before delivery and commits the policy state only after
the batch settles. Failed/uncertain effects retain the same durable batch for
the next observation; no success is inferred from mailbox admission.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest
from hyprial.daemon.impl.inbox.tracking.watchdog import InboxWatchdog, InboxAlert as InboxAlert


@dataclass(frozen=True, slots=True)
class AlertDeliveryOutcome:
    delivered: bool
    definitely_not_sent: bool = False


@dataclass(frozen=True, slots=True)
class UsageObserved:
    readings: tuple[Any, ...]
    now_ms: int


@dataclass(frozen=True, slots=True)
class UsageFailed:
    actor: str
    readings: tuple[Any, ...]
    now_ms: int


@dataclass(frozen=True, slots=True)
class FlushFailures:
    readings: tuple[Any, ...]
    now_ms: int


@dataclass(frozen=True, slots=True)
class UnfetchedObserved:
    stats: tuple[tuple[str, int, int], ...]
    running: frozenset[str]
    now_ms: int


@dataclass(frozen=True, slots=True)
class PrunedObserved:
    items: tuple[tuple[str, str], ...]
    running: frozenset[str]
    now_ms: int


@dataclass(frozen=True, slots=True)
class AlertBatch:
    operation_id: str
    after_state: bytes
    deliveries: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class RetryAlerts:
    pass


@dataclass(frozen=True, slots=True)
class WatchdogStatus:
    pending: bool
    rejected: int
    failed: int
    closed: bool


class _InboxPolicy(InboxWatchdog):
    def _save(self):
        pass


def _atomic_json(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class _WatchdogAuthority:
    def __init__(self, policy, deliver, *, capacity: int = 128, on_alert=None):
        self._policy = policy
        self._deliver = deliver
        self._path = policy._path
        self._outbox = self._path.with_suffix(".outbox.json")
        self._on_alert = on_alert
        self._guard = threading.Lock()
        self._closed = False
        self._rejected = self._failed = 0
        self._pending: AlertBatch | None = None
        self._active = False
        self._alerts: tuple[Any, ...] = ()
        self._deferred: list[object] = []
        self._capacity = capacity
        self._outstanding = 0
        self._active_credit = False
        self._generation = 1
        # Bootstrap reads precede actor exposure; all later writes are effects.
        if self._outbox.exists():
            raw = json.loads(self._outbox.read_bytes())
            self._pending = AlertBatch(
                raw["operation_id"],
                raw["after_state"].encode(),
                tuple(tuple(item) for item in raw["deliveries"]),
            )
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name=f"watchdog-{self._path.stem}",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects = EffectLane(
            name=f"{self._path.stem}-effects",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    def _admit(self, command) -> AdmissionResult:
        with self._guard:
            if self._closed:
                result = AdmissionResult.CLOSED
            elif self._outstanding >= self._capacity:
                result = AdmissionResult.OVERLOADED
            else:
                result = self._runtime.tell(self._handle, command)
            if result is AdmissionResult.ACCEPTED:
                self._outstanding += 1
            if result is not AdmissionResult.ACCEPTED:
                self._rejected += 1
            return result

    def _receive(self, command):
        if isinstance(command, RetryAlerts):
            self._start_effect()
            return
        if isinstance(command, EffectCompleted):
            if (
                self._pending is None
                or not self._active
                or command.operation_id != self._pending.operation_id
                or command.generation != self._generation
            ):
                self._effects.acknowledge(command.operation_id, command.generation)
                return
            self._active = False
            self._effects.acknowledge(command.operation_id, command.generation)
            if command.error or not command.result:
                with self._guard:
                    self._failed += 1
                return  # next observation retries the durable batch
            self._policy._state = json.loads(self._pending.after_state)
            self._pending = None
            if self._active_credit:
                with self._guard:
                    self._outstanding -= 1
                self._active_credit = False
            alerts, self._alerts = self._alerts, ()
            if self._on_alert is not None:
                for alert in alerts:
                    try:
                        self._on_alert(alert)
                    except Exception:
                        pass  # observation cannot revoke settled delivery
            if self._deferred:
                self._plan(self._deferred.pop(0))
            return
        if self._pending is not None:
            # Coalesce refresh observations, never silently merge loss events.
            if isinstance(command, (UsageObserved, FlushFailures, UnfetchedObserved)):
                previous_count = len(self._deferred)
                self._deferred = [
                    item for item in self._deferred if type(item) is not type(command)
                ]
                with self._guard:
                    self._outstanding -= previous_count - len(self._deferred)
            self._deferred.append(command)
            self._start_effect()
            return
        self._plan(command)

    def _plan(self, command):
        self._active_credit = True
        policy = self._policy
        before = copy.deepcopy(policy._state)
        deliveries = []
        policy._deliver = lambda key, text: deliveries.append((key, text)) or True
        policy._clock_ms = lambda: command.now_ms
        if isinstance(command, (UsageObserved, UsageFailed, FlushFailures)):
            policy._readings = lambda: command.readings
        try:
            if isinstance(command, UsageObserved):
                alerts = policy.observe_readings()
            elif isinstance(command, UsageFailed):
                alert = policy.observe_usage_limit_failure(command.actor)
                alerts = [alert] if alert else []
            elif isinstance(command, FlushFailures):
                alert = policy.flush_failures()
                alerts = [alert] if alert else []
            elif isinstance(command, UnfetchedObserved):
                alerts = policy.observe_unfetched(
                    command.stats, is_running=command.running.__contains__
                )
            elif isinstance(command, PrunedObserved):
                alerts = policy.observe_pruned(
                    command.items, is_running=command.running.__contains__
                )
            else:
                raise TypeError("unsupported watchdog observation")
            after = json.dumps(
                policy._state, ensure_ascii=False, sort_keys=True
            ).encode()
        finally:
            policy._state = before
        self._alerts = tuple(alerts)
        self._pending = AlertBatch(uuid4().hex, after, tuple(deliveries))
        self._start_effect()

    def _start_effect(self):
        if self._active or self._pending is None:
            return
        generation = self._generation + 1
        result = self._effects.submit(
            EffectRequest(self._pending.operation_id, generation, self._pending)
        )
        self._active = result is AdmissionResult.ACCEPTED
        if self._active:
            # The durable batch identity stays stable, but each execution has a
            # distinct token. A queued completion from a failed prior attempt
            # cannot acknowledge or alter the next attempt's live custody.
            self._generation = generation

    def _execute(self, batch: AlertBatch) -> bool:
        if self._outbox.exists():
            raw = json.loads(self._outbox.read_bytes())
            if raw["operation_id"] != batch.operation_id:
                raise RuntimeError("watchdog outbox operation conflict")
        else:
            raw = {
                "operation_id": batch.operation_id,
                "key_scope": batch.operation_id,
                "after_state": batch.after_state.decode(),
                "deliveries": batch.deliveries,
                "completed": 0,
                "attempt": 0,
            }
            _atomic_json(self._outbox, json.dumps(raw).encode())
        for index in range(raw["completed"], len(batch.deliveries)):
            key, text = batch.deliveries[index]
            # Each new alert batch is a distinct business delivery. Retries
            # and restart replay keep its persisted identity. Old outboxes
            # without a scope retain their original key to avoid duplication.
            scope = raw.get("key_scope")
            key = key if scope is None else f"{key}:batch:{scope}"
            attempt = raw["attempt"]
            delivery_key = key if not attempt else f"{key}:actor-retry-{attempt}"
            outcome = self._deliver(delivery_key, text)
            delivered = (
                outcome.delivered if isinstance(outcome, AlertDeliveryOutcome)
                else outcome is True
            )
            if not delivered:
                # Receipt timeout is uncertainty, not failed delivery. A new
                # identity is safe only with proof that the prior attempt never
                # reached the native send boundary. Legacy false is uncertain.
                if isinstance(outcome, AlertDeliveryOutcome) and outcome.definitely_not_sent:
                    raw["attempt"] += 1
                    _atomic_json(self._outbox, json.dumps(raw).encode())
                return False
            raw["completed"] = index + 1
            raw["attempt"] = 0
            _atomic_json(self._outbox, json.dumps(raw).encode())
        _atomic_json(self._path, batch.after_state)
        self._outbox.unlink(missing_ok=True)
        return True

    def status(self):
        with self._guard:
            return WatchdogStatus(
                self._pending is not None, self._rejected, self._failed, self._closed
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        self._runtime.tell(self._handle, RetryAlerts())
        while True:
            state = self._runtime.snapshot(self._handle)
            with self._guard:
                outstanding = self._outstanding
            if state.queued == state.in_flight == 0 and outstanding == 0:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))


class AsyncQuotaWatchdog(_WatchdogAuthority):
    def __init__(self, *, state_dir, deliver, readings, clock_ms, on_alert=None, evaluator=None):
        if evaluator is None:
            raise ValueError("AsyncQuotaWatchdog requires a quota evaluator when enabled")
        self._readings_source = readings
        self._clock_source = clock_ms
        evaluator._save = lambda: None  # persistence belongs to AlertBatch effects
        super().__init__(evaluator, deliver, on_alert=on_alert)

    def observe_readings(self):
        self._admit(UsageObserved(self._readings_snapshot(), self._clock_source()))
        return []

    def observe_usage_limit_failure(self, actor):
        self._admit(UsageFailed(actor, self._readings_snapshot(), self._clock_source()))
        return None

    def flush_failures(self):
        self._admit(FlushFailures(self._readings_snapshot(), self._clock_source()))
        return None

    def _readings_snapshot(self):
        # Only frozen quota windows enter the mailbox, not mutable usage extras.
        return tuple(
            replace(item, extra=MappingProxyType({}))
            for item in self._readings_source()
        )


class AsyncInboxWatchdog(_WatchdogAuthority):
    def __init__(self, *, state_dir, deliver, clock_ms, on_alert=None):
        self._clock_source = clock_ms
        super().__init__(
            _InboxPolicy(state_dir=state_dir, deliver=deliver, clock_ms=clock_ms),
            deliver,
            on_alert=on_alert,
        )

    def observe_unfetched(self, stats, *, is_running):
        stats = tuple(stats)
        self._admit(
            UnfetchedObserved(
                stats,
                frozenset(row[0] for row in stats if is_running(row[0])),
                self._clock_source(),
            )
        )
        return []

    def observe_pruned(self, items, *, is_running):
        items = tuple(items)
        self._admit(
            PrunedObserved(
                items,
                frozenset(row[0] for row in items if is_running(row[0])),
                self._clock_source(),
            )
        )
        return []
