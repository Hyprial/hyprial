from __future__ import annotations

import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .contracts import (
    ActorBackend,
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSnapshot,
    ActorSpec,
    ActorState,
    AdmissionResult,
    DrainReport,
    EventSink,
)
from .policies import SupervisionPolicy
from .scheduler import GenerationScheduler


@dataclass(slots=True)
class _ChildRecord:
    handle: ActorHandle
    spec: ActorSpec
    policy: SupervisionPolicy
    endpoint: object | None
    state: ActorState = ActorState.RUNNING
    generation: int = 1
    failures: deque[float] = field(default_factory=deque)


class ActorGuardian:
    """Daemon-owned authority for child lifecycle and restart decisions."""

    def __init__(
        self,
        *,
        policies: Mapping[str, SupervisionPolicy],
        event_sink: EventSink,
        scheduler: GenerationScheduler,
        random_source: Callable[[], float] = random.random,
        backend: ActorBackend,
    ) -> None:
        self._policies = policies
        self._event_sink = event_sink
        self._scheduler = scheduler
        self._random_source = random_source
        self._backend = backend
        self._lock = threading.RLock()
        self._children: dict[str, _ChildRecord] = {}
        self._draining = False

    def start(self, spec: ActorSpec) -> ActorHandle:
        try:
            policy = self._policies[spec.supervision_profile]
        except KeyError as exc:
            raise ValueError(
                f"unknown supervision profile: {spec.supervision_profile}"
            ) from exc
        with self._lock:
            if self._draining:
                raise RuntimeError("actor runtime is draining")
        handle = ActorHandle(actor_id=uuid.uuid4().hex, name=spec.name)
        handler = spec.handler_factory()
        endpoint = self._backend.start(
            handle=handle,
            generation=1,
            handler=handler,
            mailbox_capacity=spec.mailbox_capacity,
            event_sink=self._emit,
            failure_callback=self._child_failed,
        )
        record = _ChildRecord(
            handle=handle,
            spec=spec,
            policy=policy,
            endpoint=endpoint,
        )
        with self._lock:
            if self._draining:
                self._backend.close_admission(endpoint)
                self._backend.stop(endpoint, 0.0)
                raise RuntimeError("actor runtime started draining during start")
            self._children[handle.actor_id] = record
        return handle

    def tell(self, handle: ActorHandle, command: object) -> AdmissionResult:
        with self._lock:
            record = self._children.get(handle.actor_id)
            if (
                self._draining
                or record is None
                or record.handle != handle
                or record.state is not ActorState.RUNNING
                or record.endpoint is None
            ):
                return AdmissionResult.CLOSED
            endpoint = record.endpoint
        return self._backend.tell(endpoint, command)

    def snapshot(self, handle: ActorHandle) -> ActorSnapshot:
        with self._lock:
            record = self._require_child(handle)
            endpoint = record.endpoint
            queued, in_flight = (
                self._backend.load(endpoint) if endpoint is not None else (0, 0)
            )
            self._prune_failures(record, time.monotonic())
            return ActorSnapshot(
                handle=record.handle,
                state=record.state,
                generation=record.generation,
                queued=queued,
                in_flight=in_flight,
                failures_in_window=len(record.failures),
            )

    def stop(self, handle: ActorHandle, timeout: float) -> bool:
        with self._lock:
            record = self._require_child(handle)
            record.generation += 1
            record.state = ActorState.STOPPED
            endpoint = record.endpoint
            self._scheduler.cancel(handle.actor_id)
        stopped = endpoint is None or self._backend.stop(endpoint, timeout)
        if stopped and endpoint is not None:
            with self._lock:
                if record.endpoint is endpoint:
                    record.endpoint = None
        self._emit(
            ActorEvent(
                kind=ActorEventKind.CHILD_STOPPED,
                handle=handle,
                generation=record.generation,
            )
        )
        return stopped

    def reset(self, handle: ActorHandle) -> bool:
        with self._lock:
            record = self._require_child(handle)
            if self._draining or record.state is not ActorState.QUARANTINED:
                return False
            record.failures.clear()
            record.generation += 1
            generation = record.generation
            record.state = ActorState.RESTART_PENDING
        return self._scheduler.schedule(
            handle.actor_id,
            generation,
            0.0,
            lambda scheduled_generation: self._restart(
                handle.actor_id, scheduled_generation
            ),
        )

    def drain(self, timeout: float) -> DrainReport:
        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        with self._lock:
            self._draining = True
            records = tuple(self._children.values())
            for record in records:
                if record.endpoint is not None:
                    self._backend.close_admission(record.endpoint)
                self._scheduler.cancel(record.handle.actor_id)

        while time.monotonic() < deadline:
            if all(self._record_load(record) == (0, 0) for record in records):
                break
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

        for record in records:
            endpoint = record.endpoint
            if endpoint is None:
                continue
            if self._backend.load(endpoint) != (0, 0):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if self._backend.stop(endpoint, remaining):
                with self._lock:
                    if record.endpoint is endpoint:
                        record.endpoint = None
                        record.state = ActorState.STOPPED

        remaining_handles = tuple(
            record.handle for record in records if record.endpoint is not None
        )
        return DrainReport(
            complete=not remaining_handles,
            elapsed=time.monotonic() - started,
            remaining=remaining_handles,
        )

    def _child_failed(
        self,
        actor_id: str,
        generation: int,
        failure: BaseException,
    ) -> None:
        self._process_failure(
            actor_id,
            generation,
            failure,
            expected_state=ActorState.RUNNING,
        )

    def _restart_attempt_failed(
        self,
        actor_id: str,
        generation: int,
        failure: BaseException,
    ) -> None:
        self._process_failure(
            actor_id,
            generation,
            failure,
            expected_state=ActorState.RESTART_PENDING,
        )

    def _process_failure(
        self,
        actor_id: str,
        generation: int,
        failure: BaseException,
        *,
        expected_state: ActorState,
    ) -> None:
        now = time.monotonic()
        scheduled: tuple[int, float] | None = None
        scheduled_event: ActorEvent | None = None
        quarantine_event: ActorEvent | None = None
        with self._lock:
            record = self._children.get(actor_id)
            if (
                record is None
                or record.generation != generation
                or record.state is not expected_state
            ):
                return
            record.endpoint = None
            record.failures.append(now)
            self._prune_failures(record, now)
            failed_event = ActorEvent(
                kind=ActorEventKind.CHILD_FAILED,
                handle=record.handle,
                generation=generation,
                code=type(failure).__name__,
                detail=str(failure),
            )
            if self._draining:
                record.state = ActorState.STOPPED
            elif len(record.failures) > record.policy.max_restarts:
                record.state = ActorState.QUARANTINED
                quarantine_event = ActorEvent(
                    kind=ActorEventKind.CHILD_QUARANTINED,
                    handle=record.handle,
                    generation=generation,
                    code=type(failure).__name__,
                    detail=str(failure),
                )
            else:
                record.generation += 1
                record.state = ActorState.RESTART_PENDING
                delay = record.policy.delay(len(record.failures), self._random_source())
                scheduled = (record.generation, delay)
                scheduled_event = ActorEvent(
                    kind=ActorEventKind.RESTART_SCHEDULED,
                    handle=record.handle,
                    generation=record.generation,
                    restart_delay=delay,
                )
        self._emit(failed_event)
        if quarantine_event is not None:
            self._emit(quarantine_event)
        if scheduled is not None:
            next_generation, delay = scheduled
            assert scheduled_event is not None
            self._emit(scheduled_event)
            self._scheduler.schedule(
                actor_id,
                next_generation,
                delay,
                lambda scheduled_generation: self._restart(
                    actor_id, scheduled_generation
                ),
            )

    def _restart(self, actor_id: str, generation: int) -> None:
        with self._lock:
            record = self._children.get(actor_id)
            if (
                self._draining
                or record is None
                or record.generation != generation
                or record.state is not ActorState.RESTART_PENDING
            ):
                return
            spec = record.spec
            handle = record.handle
        try:
            handler = spec.handler_factory()
            endpoint = self._backend.start(
                handle=handle,
                generation=generation,
                handler=handler,
                mailbox_capacity=spec.mailbox_capacity,
                event_sink=self._emit,
                failure_callback=self._child_failed,
            )
        except Exception as exc:
            self._restart_attempt_failed(actor_id, generation, exc)
            return
        stale = False
        with self._lock:
            record = self._children.get(actor_id)
            if (
                self._draining
                or record is None
                or record.generation != generation
                or record.state is not ActorState.RESTART_PENDING
            ):
                stale = True
            else:
                record.endpoint = endpoint
                record.state = ActorState.RUNNING
        if stale:
            self._backend.close_admission(endpoint)
            self._backend.stop(endpoint, 0.0)
            return
        self._emit(
            ActorEvent(
                kind=ActorEventKind.CHILD_RESTARTED,
                handle=handle,
                generation=generation,
            )
        )

    def _record_load(self, record: _ChildRecord) -> tuple[int, int]:
        endpoint = record.endpoint
        return self._backend.load(endpoint) if endpoint is not None else (0, 0)

    def _prune_failures(self, record: _ChildRecord, now: float) -> None:
        cutoff = now - record.policy.restart_window
        while record.failures and record.failures[0] < cutoff:
            record.failures.popleft()

    def _require_child(self, handle: ActorHandle) -> _ChildRecord:
        record = self._children.get(handle.actor_id)
        if record is None or record.handle != handle:
            raise KeyError(f"unknown actor handle: {handle.actor_id}")
        return record

    def _emit(self, event: ActorEvent) -> None:
        try:
            self._event_sink(event)
        except Exception:
            return
