"""Bounded progress-publication effects with actor-held completion custody."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest
from hyprial.daemon.impl.inbox.tracking.progress import (
    ProgressEvent, decode_progress_event, encode_progress_event,
)


@dataclass(frozen=True, slots=True)
class PublishProgress:
    operation_id: str
    generation: int
    payload: bytes
    recipient: str


@dataclass(frozen=True, slots=True)
class _TakePublished:
    operation_id: str


@dataclass(frozen=True, slots=True)
class ProgressPublishProjection:
    pending: int
    published: int
    rejected: int
    failed: int


@dataclass(slots=True)
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    value: int = 0


class ProgressPublishCoordinator:
    def __init__(
        self, submit, *, capacity: int = 64, workers: int = 4,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._submit = submit
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._active: set[str] = set()
        self._replies: dict[str, _Reply] = {}
        self._published = self._unread_published = 0
        self._rejected = self._failed = 0
        self._capacity = capacity
        self._closed = False
        self._handle = self._runtime.start(
            ActorSpec(
                name="dispatch-progress",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects: EffectLane[PublishProgress, bool] = EffectLane(
            name="dispatch-progress-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=workers,
        )

    def submit(self, event: ProgressEvent, recipient: str) -> AdmissionResult:
        command = PublishProgress(
            uuid.uuid4().hex, 1, encode_progress_event(event), recipient
        )
        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
            if len(self._active) >= self._capacity:
                self._rejected += 1
                return AdmissionResult.OVERLOADED
            self._active.add(command.operation_id)
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._active.discard(command.operation_id)
                self._rejected += 1
            return admitted

    def _execute(self, command: PublishProgress) -> bool:
        event = decode_progress_event(command.payload)
        if event is None:
            raise ValueError("invalid admitted progress event")
        return bool(self._submit(event, recipient=command.recipient))

    def _receive(self, command: object) -> None:
        if isinstance(command, PublishProgress):
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                with self._guard:
                    self._active.discard(command.operation_id)
                    self._rejected += 1
            return
        if isinstance(command, EffectCompleted):
            with self._guard:
                if command.operation_id not in self._active:
                    self._effects.acknowledge(command.operation_id, command.generation)
                    return
                self._active.remove(command.operation_id)
                if command.result is True:
                    self._published += 1
                    self._unread_published += 1
                else:
                    self._failed += 1
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        if isinstance(command, _TakePublished):
            with self._guard:
                count = self._unread_published
                self._unread_published = 0
                reply = self._replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.value = count
                    reply.ready.set()
            return
        raise TypeError("unsupported progress publication command")

    def take_published(self) -> int:
        command = _TakePublished(uuid.uuid4().hex)
        reply = _Reply()
        with self._guard:
            self._replies[command.operation_id] = reply
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._replies.pop(command.operation_id)
                return 0
        if not reply.ready.wait(1.0):
            return 0
        return reply.value

    def projection(self) -> ProgressPublishProjection:
        with self._guard:
            return ProgressPublishProjection(
                len(self._active), self._published, self._rejected, self._failed
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._active)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
