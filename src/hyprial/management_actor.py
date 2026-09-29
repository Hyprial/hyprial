"""Management saga admission and adapter-removal leases.

Caller-side file compensation retains an opaque lease, never an application
mutex. Registry operations execute on one bounded effect lane through existing
Agent/Harness ports; only the management actor releases the lease.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectLane, EffectRequest, EffectCompleted
from hyprial.management import (
    AdapterRegistrySnapshot,
    AdapterRemovalTransaction,
    EnsureSquireRegistryCommand,
    ManagementError,
    RegistryManagementHandler,
    SquireRegistryResult,
)


@dataclass(frozen=True, slots=True)
class BeginAdapterRemoval:
    lease: str
    name: str


@dataclass(frozen=True, slots=True)
class CommitAdapterRemoval:
    lease: str


@dataclass(frozen=True, slots=True)
class RollbackAdapterRemoval:
    lease: str


@dataclass(frozen=True, slots=True)
class EndAdapterRemoval:
    lease: str


@dataclass(frozen=True, slots=True)
class AbandonAdapterRemoval:
    lease: str


@dataclass(frozen=True, slots=True)
class ManagementRequest:
    operation_id: str
    payload: (
        EnsureSquireRegistryCommand
        | BeginAdapterRemoval
        | CommitAdapterRemoval
        | RollbackAdapterRemoval
        | EndAdapterRemoval
    )


@dataclass(frozen=True, slots=True)
class ManagementOutcome:
    snapshot: AdapterRegistrySnapshot | None = None
    payload: bytes | None = None
    error: str | None = None


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: ManagementOutcome | None = None
    error: BaseException | None = None


class _RemovalLease:
    def __init__(self, owner, token, snapshot):
        self._owner = owner
        self._token = token
        self.snapshot = snapshot
        self.committed = False

    def commit(self):
        self._owner._call(CommitAdapterRemoval(self._token))
        self.committed = True

    def rollback(self):
        self._owner._call(RollbackAdapterRemoval(self._token))
        self.committed = False


class RegistryManagementAuthority:
    def __init__(
        self, harnesses, agents, *, start_harness=None, capacity=64, timeout=30.0
    ):
        self._harnesses = harnesses
        self._agents = agents
        self._handler = RegistryManagementHandler(
            harnesses, agents, start_harness=start_harness
        )
        self._transactions = {}  # only the effect executor may access these
        self._guard = threading.Lock()
        self._pending = {}
        self._errors = {}
        self._active_lease = None
        self._deferred = []
        self._abandoned = set()
        self._cleanup_leases = set()
        self._closed = False
        self._capacity = capacity
        self._timeout = timeout
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="registry-management",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity + 4,
            )
        )
        # Control commands retain reserved room to commit/rollback/end a lease
        # even when unrelated requests fill the normal admission window.
        self._effects = EffectLane(
            name="registry-management-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity + 4,
        )

    def _call(self, payload):
        control = isinstance(
            payload, (CommitAdapterRemoval, RollbackAdapterRemoval, EndAdapterRemoval)
        )
        request = ManagementRequest(uuid4().hex, payload)
        reply = _Reply()
        with self._guard:
            if (self._closed and not control) or len(
                self._pending
            ) >= self._capacity + (4 if control else 0):
                raise ManagementError("management authority closed or overloaded")
            admission = self._effects.reserve(request.operation_id, 1)
            if admission is not AdmissionResult.ACCEPTED:
                raise ManagementError(f"management effects {admission.value}")
            self._pending[request.operation_id] = (request, reply)
            admission = self._runtime.tell(self._handle, request)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(request.operation_id)
                self._effects.cancel_reservation(request.operation_id, 1)
                raise ManagementError(f"management admission {admission.value}")
        if not reply.ready.wait(self._timeout):
            if isinstance(payload, BeginAdapterRemoval):
                # No file mutation can have started because __enter__ never
                # returned. Request ordered lease cleanup; this is not a claim
                # that the already-accepted snapshot operation was canceled.
                with self._guard:
                    if (
                        request.operation_id in self._pending
                        or self._active_lease == payload.lease
                    ):
                        self._abandoned.add(payload.lease)
                self._runtime.tell(self._handle, AbandonAdapterRemoval(payload.lease))
            raise TimeoutError(
                f"management operation {request.operation_id} remains accepted"
            )
        if reply.error is not None:
            raise reply.error
        if reply.result.error is not None:
            raise ManagementError(reply.result.error)
        return reply.result

    def _receive(self, event):
        try:
            if not isinstance(event, AbandonAdapterRemoval):
                self._receive_message(event)
        finally:
            self._retire_abandoned()

    def _receive_message(self, event):
        if isinstance(event, ManagementRequest):
            self._dispatch(event)
            return
        if not isinstance(event, EffectCompleted):
            raise TypeError("unsupported management command")
        with self._guard:
            pending = self._pending.pop(event.operation_id, None)
            error = self._errors.pop(event.operation_id, None)
        if pending is None:
            self._effects.acknowledge(event.operation_id, event.generation)
            return
        request, reply = pending
        result = event.result or ManagementOutcome(error=event.error)
        if (
            isinstance(request.payload, BeginAdapterRemoval)
            and result.error is not None
        ):
            # A failed snapshot owns no file-mutation lease. Retire a timeout's
            # cleanup marker even if its wake command could not be admitted.
            with self._guard:
                self._abandoned.discard(request.payload.lease)
        release = isinstance(request.payload, EndAdapterRemoval) or (
            isinstance(request.payload, BeginAdapterRemoval)
            and result.error is not None
        )
        release = release and request.payload.lease == self._active_lease
        if release:
            with self._guard:
                self._active_lease = None
        if isinstance(request.payload, EndAdapterRemoval):
            with self._guard:
                self._abandoned.discard(request.payload.lease)
                self._cleanup_leases.discard(request.payload.lease)
        self._effects.acknowledge(event.operation_id, event.generation)
        reply.result = result
        reply.error = error
        reply.ready.set()
        if release:
            while self._deferred and self._active_lease is None:
                self._dispatch(self._deferred.pop(0))

    def _dispatch(self, request):
        payload = request.payload
        with self._guard:
            abandoned = (
                isinstance(payload, BeginAdapterRemoval)
                and payload.lease in self._abandoned
            )
        if abandoned:
            self._effects.cancel_reservation(request.operation_id, 1)
            with self._guard:
                _, reply = self._pending.pop(request.operation_id)
                self._abandoned.discard(payload.lease)
            reply.result = ManagementOutcome(
                error="adapter lease abandoned before snapshot"
            )
            reply.ready.set()
            return
        normal = isinstance(payload, (EnsureSquireRegistryCommand, BeginAdapterRemoval))
        if normal and self._active_lease is not None:
            self._deferred.append(request)
            return
        if not normal and payload.lease != self._active_lease:
            self._effects.cancel_reservation(request.operation_id, 1)
            with self._guard:
                _, reply = self._pending.pop(request.operation_id)
            reply.result = ManagementOutcome(error="stale adapter removal lease")
            reply.ready.set()
            return
        if isinstance(payload, BeginAdapterRemoval):
            with self._guard:
                self._active_lease = payload.lease
        self._effects.submit_reserved(EffectRequest(request.operation_id, 1, request))

    def _retire_abandoned(self):
        remaining = []
        for request in self._deferred:
            with self._guard:
                abandoned = (
                    isinstance(request.payload, BeginAdapterRemoval)
                    and request.payload.lease in self._abandoned
                )
            if abandoned:
                self._dispatch(request)
            else:
                remaining.append(request)
        self._deferred = remaining
        with self._guard:
            token = self._active_lease
            if token not in self._abandoned or token in self._cleanup_leases:
                return
            request = ManagementRequest(uuid4().hex, EndAdapterRemoval(token))
            if (
                self._effects.reserve(request.operation_id, 1)
                is not AdmissionResult.ACCEPTED
            ):
                return  # another completion will retry the retained cleanup
            self._pending[request.operation_id] = (request, _Reply())
            self._cleanup_leases.add(token)
        self._dispatch(request)

    def status(self):
        with self._guard:
            return {
                "pending": len(self._pending),
                "leaseActive": self._active_lease is not None,
                "closed": self._closed,
            }

    def _execute(self, request):
        payload = request.payload
        try:
            if isinstance(payload, EnsureSquireRegistryCommand):
                value = self._handler.ensure_squire(payload)
                return ManagementOutcome(
                    payload=json.dumps(value.to_payload()).encode()
                )
            if isinstance(payload, BeginAdapterRemoval):
                snapshot = AdapterRegistrySnapshot(
                    self._harnesses.snapshot_adapter_registration(payload.name),
                    self._agents.pins().get(payload.name),
                )
                self._transactions[payload.lease] = AdapterRemovalTransaction(
                    self._harnesses, self._agents, snapshot
                )
                return ManagementOutcome(snapshot=snapshot)
            if isinstance(payload, EndAdapterRemoval):
                self._transactions.pop(payload.lease, None)
                return ManagementOutcome()
            transaction = self._transactions[payload.lease]
            if isinstance(payload, CommitAdapterRemoval):
                transaction.commit()
            elif isinstance(payload, RollbackAdapterRemoval):
                transaction.rollback()
            return ManagementOutcome()
        except BaseException as error:
            with self._guard:
                self._errors[request.operation_id] = error
            return ManagementOutcome(error=type(error).__name__)

    def ensure_squire(self, command):
        result = self._call(command)
        return SquireRegistryResult.from_payload(json.loads(result.payload))

    @contextmanager
    def adapter_removal(self, name):
        token = uuid4().hex
        snapshot = self._call(BeginAdapterRemoval(token, name)).snapshot
        lease = _RemovalLease(self, token, snapshot)
        try:
            yield lease
        except BaseException as operation_error:
            try:
                lease.rollback()
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "adapter registry operation and rollback failed",
                    [operation_error, rollback_error],
                ) from operation_error
            raise
        finally:
            self._call(EndAdapterRemoval(token))

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                active = bool(self._pending) or self._active_lease is not None
            if not active:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
