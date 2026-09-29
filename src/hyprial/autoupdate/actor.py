"""Actor-owned update scheduling with isolated policy/read/run effects."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectLane, EffectRequest, EffectCompleted
from hyprial.actor_runtime.scheduler import GenerationScheduler
from . import InProcessAutoUpdateScheduler, next_scheduled_run, read_last_run, updates


@dataclass(frozen=True, slots=True)
class CalendarObserved:
    now: datetime


@dataclass(frozen=True, slots=True)
class TriggerUpdate:
    reason: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ObservePolicy:
    operation_id: str


@dataclass(slots=True)
class _PolicyRead:
    ready: threading.Event = field(default_factory=threading.Event)
    enabled: bool | None = None
    error: str | None = None
    abandoned: bool = False


@dataclass(frozen=True, slots=True)
class UpdateEffect:
    reason: str | None


@dataclass(frozen=True, slots=True)
class UpdateCompleted:
    enabled: bool
    last_run: bytes
    exit_code: int | None = None
    skip_reason: str | None = None


class AutoUpdateAuthority:
    def __init__(self, **options):
        self._policy = InProcessAutoUpdateScheduler(**options)
        # Immutable composition fact; policy file reads remain on the effect
        # worker even when a caller asks for a fresh observation.
        self._hyprial_home = self._policy._hyprial_home
        self._guard = threading.Lock()
        self._policy_reads: dict[str, _PolicyRead] = {}
        self._policy_read_capacity = 8
        self._started = False
        self._closed = False
        self._reserved = False
        self._active = False
        self._next_run = None
        self._last_triggered = None
        self._last_reason = None
        self._last_exit_code = None
        self._last_skip_reason = None
        self._last_error = None
        self._enabled = None
        self._last_run = b"null"
        self._sequence = 0
        self._refreshing = False
        self._run_operation = None
        self._policy_operation = None
        self._queued_trigger = None
        self._runtime = ActorRuntime()
        self._scheduler = GenerationScheduler()
        handle = None
        try:
            handle = self._runtime.start(
                ActorSpec(
                    name="auto-update",
                    handler_factory=lambda: self._receive,
                    mailbox_capacity=8,
                )
            )
            self._handle = handle
            self._effects = EffectLane(
                name="hyprial-autoupdate",
                execute=self._execute,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=2,
            )
        except BaseException as error:
            if handle is not None:
                try:
                    if not self._runtime.stop(handle, 5.0):
                        error.add_note(
                            "auto-update actor did not stop during construction rollback"
                        )
                except BaseException as cleanup_error:
                    error.add_note(
                        "auto-update actor rollback raised "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            try:
                if not self._scheduler.shutdown(5.0):
                    error.add_note(
                        "auto-update scheduler did not drain during construction rollback"
                    )
            except BaseException as cleanup_error:
                error.add_note(
                    "auto-update scheduler rollback raised "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def start(self):
        with self._guard:
            if self._started or self._closed:
                return
            self._started = True
        self._runtime.tell(self._handle, CalendarObserved(self._policy.now()))

    def trigger(self, reason="manual"):
        with self._guard:
            if self._closed or self._reserved:
                return False
            self._reserved = True
            admission = self._runtime.tell(
                self._handle, TriggerUpdate(reason, self._policy.now())
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._reserved = False
            return admission is AdmissionResult.ACCEPTED

    def _auto_upgrade_enabled(self, *, timeout: float = 2.0) -> bool:
        """Compatibility read through the existing bounded policy effect owner."""

        operation_id = f"policy-read:{uuid4().hex}"
        reply = _PolicyRead()
        with self._guard:
            if self._closed or len(self._policy_reads) >= self._policy_read_capacity:
                raise RuntimeError("auto-update policy read is closed or overloaded")
            self._policy_reads[operation_id] = reply
        admission = self._runtime.tell(self._handle, ObservePolicy(operation_id))
        if admission is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._policy_reads.pop(operation_id, None)
            raise RuntimeError(f"auto-update policy admission is {admission.value}")
        if not reply.ready.wait(max(0.0, timeout)):
            with self._guard:
                if not reply.ready.is_set():
                    reply.abandoned = True
                    raise TimeoutError("accepted auto-update policy read is unsettled")
        with self._guard:
            self._policy_reads.pop(operation_id, None)
            result, error = reply.enabled, reply.error
        if error is not None or result is None:
            raise RuntimeError(error or "auto-update policy read had no result")
        return result

    def _receive(self, command):
        if isinstance(command, ObservePolicy):
            admission = self._effects.submit(
                EffectRequest(command.operation_id, 1, UpdateEffect(None))
            )
            if admission is not AdmissionResult.ACCEPTED:
                with self._guard:
                    reply = self._policy_reads.get(command.operation_id)
                    if reply is not None:
                        reply.error = f"auto-update policy effect {admission.value}"
                        reply.ready.set()
                        if reply.abandoned:
                            self._policy_reads.pop(command.operation_id, None)
            return
        if isinstance(command, CalendarObserved):
            with self._guard:
                if self._closed:
                    return
                if self._next_run is None:
                    self._next_run = next_scheduled_run(command.now)
                due = command.now >= self._next_run
                can_run = due and not self._reserved
                if due:
                    self._next_run = next_scheduled_run(command.now)
                if can_run:
                    self._reserved = True
                queued, self._queued_trigger = self._queued_trigger, None
            if queued is not None:
                self._begin(queued)
            elif can_run:
                self._begin(TriggerUpdate("schedule", command.now))
            else:
                self._refresh_policy()
            self._schedule()
            return
        if isinstance(command, TriggerUpdate):
            self._begin(command)
            return
        if not isinstance(command, EffectCompleted):
            raise TypeError("unsupported auto-update command")
        if command.operation_id.startswith("policy-read:"):
            with self._guard:
                reply = self._policy_reads.get(command.operation_id)
                if reply is not None:
                    result = command.result
                    if command.error is not None or result is None:
                        reply.error = command.error or "policy effect returned no result"
                    else:
                        self._enabled = result.enabled
                        reply.enabled = result.enabled
                    reply.ready.set()
                    if reply.abandoned:
                        self._policy_reads.pop(command.operation_id, None)
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        with self._guard:
            is_run = command.operation_id.startswith("run:")
            current = self._run_operation if is_run else self._policy_operation
            if command.generation != 1 or command.operation_id != current:
                self._effects.acknowledge(command.operation_id, command.generation)
                return
            value = command.result
            self._last_error = command.error
            if is_run and command.error is not None:
                self._last_exit_code = None
            if value is not None:
                self._enabled = value.enabled
                self._last_run = value.last_run
                if command.operation_id.startswith("run:"):
                    self._last_exit_code = value.exit_code
                    self._last_skip_reason = value.skip_reason
            if is_run:
                self._reserved = self._active = False
                self._run_operation = None
            else:
                self._refreshing = False
                self._policy_operation = None
        self._effects.acknowledge(command.operation_id, command.generation)

    def _begin(self, command):
        with self._guard:
            if self._closed:
                self._reserved = False
                return
            if not self._started:
                self._queued_trigger = command
                return
            self._active = True
            self._last_triggered = command.now
            self._last_reason = command.reason
            self._sequence += 1
            operation = f"run:{self._sequence}"
            self._run_operation = operation
        admission = self._effects.submit(
            EffectRequest(operation, 1, UpdateEffect(command.reason))
        )
        if admission is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._reserved = self._active = False
                self._last_skip_reason = admission.value

    def _refresh_policy(self):
        with self._guard:
            if self._closed or self._refreshing:
                return
            self._refreshing = True
            self._sequence += 1
            operation = f"policy:{self._sequence}"
            self._policy_operation = operation
        if (
            self._effects.submit(EffectRequest(operation, 1, UpdateEffect(None)))
            is not AdmissionResult.ACCEPTED
        ):
            with self._guard:
                self._refreshing = False

    def _execute(self, command):
        enabled = self._policy._auto_upgrade_enabled()
        exit_code = None
        skip_reason = None
        if command.reason is not None:
            if not enabled:
                skip_reason = updates.AUTOUPGRADE_DISABLED_REASON
                self._policy._emit(
                    "warning",
                    "autoupdate.run.skipped",
                    trigger=command.reason,
                    reason=skip_reason,
                )
            else:
                self._policy._emit(
                    "info", "autoupdate.run.started", trigger=command.reason
                )
                try:
                    result = self._policy._execute(command.reason)
                    exit_code = result.returncode
                    self._policy._emit(
                        "info" if exit_code == 0 else "error",
                        "autoupdate.run.completed",
                        trigger=command.reason,
                        exitCode=exit_code,
                    )
                except Exception as error:
                    self._policy._emit(
                        "error",
                        "autoupdate.run.failed",
                        trigger=command.reason,
                        errorType=type(error).__name__,
                    )
        return UpdateCompleted(
            enabled,
            json.dumps(read_last_run(self._policy.state_dir)).encode(),
            exit_code,
            skip_reason,
        )

    def _schedule(self):
        with self._guard:
            if self._closed or not self._started:
                return
            self._scheduler.schedule(
                "calendar",
                1,
                self._policy.calendar_recheck_seconds,
                lambda generation: self._runtime.tell(
                    self._handle, CalendarObserved(self._policy.now())
                ),
            )

    def status(self):
        with self._guard:
            return {
                "running": self._started and not self._closed,
                "active": self._active,
                "pending": self._reserved and not self._active,
                "nextRunAt": self._next_run.isoformat() if self._next_run else None,
                "lastTriggeredAt": self._last_triggered.isoformat()
                if self._last_triggered
                else None,
                "lastTrigger": self._last_reason,
                "lastExitCode": self._last_exit_code,
                "autoUpgradeEnabled": self._enabled,
                "lastSkipReason": self._last_skip_reason,
                "lastRun": json.loads(self._last_run),
                "lastError": self._last_error,
            }

    def stop(self, *, timeout=0.25):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
            if not self._started:
                self._queued_trigger = None
                self._reserved = False
        if not self._scheduler.shutdown(max(0.0, deadline - time.monotonic())):
            return False
        while True:
            snapshot = self._runtime.snapshot(self._handle)
            with self._guard:
                active = self._reserved or self._refreshing
            if not active and snapshot.queued == snapshot.in_flight == 0:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
