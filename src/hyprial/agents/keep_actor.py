"""Typed keep-list mutations with cache-only daemon projections."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectLane, EffectRequest, EffectCompleted
from .activity import AgentKeepList, AgentKeepListError


@dataclass(frozen=True, slots=True)
class KeepChanged:
    actor: str
    keep: bool


@dataclass(frozen=True, slots=True)
class RefreshKeepList:
    pass


@dataclass(frozen=True, slots=True)
class KeepCommand:
    operation_id: str
    payload: KeepChanged | RefreshKeepList


@dataclass(frozen=True, slots=True)
class KeepResult:
    actors: tuple[str, ...]
    changed: bool
    error: str | None = None


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: KeepResult | None = None


class AgentKeepListAuthority:
    def __init__(self, path, *, normalize, capacity=64, timeout=5.0):
        self.path = path
        self._store = AgentKeepList(path, normalize=normalize)
        self._actors = self._store.list()
        self._guard = threading.Lock()
        self._closed = False
        self._pending = {}
        self._timeout = timeout
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="agent-keep-list",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects = EffectLane(
            name="agent-keep-storage",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    def _execute(self, payload):
        try:
            changed = False
            if isinstance(payload, KeepChanged):
                changed = (
                    self._store.add(payload.actor)
                    if payload.keep
                    else self._store.remove(payload.actor)
                )
            elif not isinstance(payload, RefreshKeepList):
                raise TypeError("unsupported keep mutation")
            return KeepResult(self._store.list(), changed)
        except AgentKeepListError as error:
            return KeepResult((), False, str(error))

    def _receive(self, event):
        if isinstance(event, KeepCommand):
            self._effects.submit_reserved(
                EffectRequest(event.operation_id, 1, event.payload)
            )
            return
        if not isinstance(event, EffectCompleted):
            raise TypeError("unsupported keep authority message")
        with self._guard:
            reply = self._pending.pop(event.operation_id, None)
            if reply is not None:
                result = event.result or KeepResult((), False, event.error)
                if result.error is None:
                    self._actors = result.actors
                reply.result = result
        self._effects.acknowledge(event.operation_id, event.generation)
        if reply is not None:
            reply.ready.set()

    def _call(self, payload, *, wait=True):
        command = KeepCommand(uuid4().hex, payload)
        reply = _Reply()
        with self._guard:
            if self._closed:
                raise AgentKeepListError("keep authority is closed")
            admission = self._effects.reserve(command.operation_id, 1)
            if admission is not AdmissionResult.ACCEPTED:
                raise AgentKeepListError(f"keep authority {admission.value}")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                self._effects.cancel_reservation(command.operation_id, 1)
                raise AgentKeepListError(f"keep admission {admission.value}")
        if not wait:
            return None
        if not reply.ready.wait(self._timeout):
            raise TimeoutError(
                f"keep operation {command.operation_id} remains accepted"
            )
        if reply.result.error is not None:
            raise AgentKeepListError(reply.result.error)
        return reply.result.changed

    def list(self):
        with self._guard:
            return self._actors

    def add(self, actor):
        return self._call(KeepChanged(actor, True))

    def remove(self, actor):
        return self._call(KeepChanged(actor, False))

    def refresh(self, *, wait=False):
        return self._call(RefreshKeepList(), wait=wait)

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
