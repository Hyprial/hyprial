"""Typed transport lifetime authority above the native Zenoh adapter.

The native adapter keeps #891's lock/RX emergency mechanics. This owner fences
its generations, orders registration/rebuild/close against accepted operations,
and keeps native calls off its mailbox. Caller deadlines never cancel custody.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.scheduler import GenerationScheduler
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest
from .api import TransportSample


@dataclass(frozen=True, slots=True)
class Publish:
    key: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class Query:
    key: str
    payload: bytes | None
    timeout: float
    all_replies: bool


@dataclass(frozen=True, slots=True)
class Declare:
    token: str
    kind: str
    key: str
    history: bool = True


@dataclass(frozen=True, slots=True)
class Retire:
    token: str


@dataclass(frozen=True, slots=True)
class Reconfigure:
    endpoints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangeRebuildHook:
    token: str
    add: bool


@dataclass(frozen=True, slots=True)
class RefreshCallbacks:
    pass


@dataclass(frozen=True, slots=True)
class RecoverCallbacks:
    pass


@dataclass(frozen=True, slots=True)
class CloseSession:
    pass


@dataclass(frozen=True, slots=True)
class SessionCommand:
    operation_id: str
    payload: (
        Publish
        | Query
        | Declare
        | Retire
        | Reconfigure
        | ChangeRebuildHook
        | CloseSession
        | RefreshCallbacks
    )


@dataclass(frozen=True, slots=True)
class SessionEffect:
    command: SessionCommand
    generation: int
    declarations: tuple[Declare, ...] = ()
    hooks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryResult:
    samples: tuple[TransportSample, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionProjection:
    generation: int
    pending: int
    registrations: int
    rejected: int
    stale_callbacks: int
    closed: bool
    callbacks_complete: bool = True


@dataclass(frozen=True, slots=True)
class CallbackObserved:
    generation: int
    sample: TransportSample


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


class _RegistrationPort:
    def __init__(self, owner, token):
        self._owner = owner
        self._token = token

    def close(self):
        self._owner._call(Retire(self._token))


@dataclass(frozen=True, slots=True)
class _CallbackWork:
    token: str
    observed: CallbackObserved


@dataclass(slots=True)
class _CallbackSlot:
    queued: deque[EffectRequest[_CallbackWork]] = field(default_factory=deque)
    current: str | None = None
    outstanding: int = 0
    accepting: bool = True


@dataclass(frozen=True, slots=True)
class CallbackPoolSnapshot:
    registrations: int
    outstanding: int
    retired: int
    workers: int
    closing: bool


class _CallbackPool:
    """Finite registration cells over four workers, with FIFO per token.

    Native RX only copies a sample and reserves a bounded credit. Exactly one
    work item per token may enter the effect lane; the remaining accepted
    items keep their EffectLane reservations until their predecessor settles.
    """

    def __init__(
        self,
        invoke: Callable[[str, CallbackObserved], None],
        invalidate: Callable[[str, int], None],
        *,
        registrations: int,
        outstanding: int,
        per_token: int,
        workers: int,
    ) -> None:
        self._invoke = invoke
        self._invalidate = invalidate
        self._registration_capacity = registrations
        self._outstanding_capacity = outstanding
        self._per_token_capacity = per_token
        self._workers = workers
        self._guard = threading.Condition()
        self._slots: dict[str, _CallbackSlot] = {}
        self._heads: dict[str, str] = {}
        self._outstanding = 0
        self._closing = False
        # Every accepted item owns a reservation, including a queued item.
        # The lane capacity therefore covers all coordinator credits, and a
        # token's next head cannot be refused after its predecessor ACK.
        self._lane: EffectLane[_CallbackWork, None] = EffectLane(
            name="transport-callback",
            execute=self._execute,
            complete=self._complete,
            capacity=outstanding,
            workers=workers,
        )

    def register(self, token: str) -> AdmissionResult:
        with self._guard:
            if self._closing:
                return AdmissionResult.CLOSED
            if len(self._slots) >= self._registration_capacity:
                return AdmissionResult.OVERLOADED
            if token in self._slots:
                return AdmissionResult.ACCEPTED
            self._slots[token] = _CallbackSlot()
            return AdmissionResult.ACCEPTED

    def admit(self, token: str, observed: CallbackObserved) -> AdmissionResult:
        request = EffectRequest(
            uuid4().hex, observed.generation, _CallbackWork(token, observed)
        )
        with self._guard:
            slot = self._slots.get(token)
            if self._closing or slot is None or not slot.accepting:
                return AdmissionResult.CLOSED
            if (
                self._outstanding >= self._outstanding_capacity
                or slot.outstanding >= self._per_token_capacity
            ):
                return AdmissionResult.OVERLOADED
            reserved = self._lane.reserve(request.operation_id, request.generation)
            if reserved is not AdmissionResult.ACCEPTED:
                return reserved
            if slot.current is None:
                submitted = self._lane.submit_reserved(request)
                if submitted is not AdmissionResult.ACCEPTED:
                    self._lane.cancel_reservation(
                        request.operation_id, request.generation
                    )
                    return submitted
                slot.current = request.operation_id
                self._heads[request.operation_id] = token
            else:
                slot.queued.append(request)
            slot.outstanding += 1
            self._outstanding += 1
            return AdmissionResult.ACCEPTED

    def _execute(self, work: _CallbackWork) -> None:
        self._invoke(work.token, work.observed)

    def _complete(self, event: EffectCompleted[None]) -> AdmissionResult:
        failed: tuple[str, int] | None = None
        with self._guard:
            token = self._heads.get(event.operation_id)
            if token is None:
                # A duplicate completion can only be an EffectLane retry after
                # the exact ACK has already settled; never run the handler twice.
                return AdmissionResult.ACCEPTED
            slot = self._slots[token]
            if slot.current != event.operation_id:
                raise RuntimeError("callback completion lost token ordering")
            if slot.queued:
                next_request = slot.queued[0]
                submitted = self._lane.submit_reserved(next_request)
                if submitted is not AdmissionResult.ACCEPTED:
                    # No ACK: the lane retries this exact completion. All
                    # queued reservations remain owned in the meantime.
                    raise RuntimeError("accepted callback head was not submitted")
            if not self._lane.acknowledge(event.operation_id, event.generation):
                raise RuntimeError("accepted callback completion was not acknowledged")
            self._heads.pop(event.operation_id)
            if slot.queued:
                slot.queued.popleft()
                slot.current = next_request.operation_id
                self._heads[next_request.operation_id] = token
            else:
                slot.current = None
            slot.outstanding -= 1
            self._outstanding -= 1
            if not slot.accepting and slot.outstanding == 0:
                self._slots.pop(token)
            self._guard.notify_all()
            if event.error is not None:
                failed = (token, event.generation)
        if failed is not None:
            self._invalidate(*failed)
        return AdmissionResult.ACCEPTED

    def retire(self, token: str) -> None:
        with self._guard:
            slot = self._slots.get(token)
            if slot is None:
                return
            slot.accepting = False
            if slot.outstanding == 0:
                self._slots.pop(token)
            self._guard.notify_all()

    def close_admission(self) -> None:
        with self._guard:
            self._closing = True

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closing = True
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return True

    def close(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        if not self.drain(max(0.0, deadline - time.monotonic())):
            return False
        with self._guard:
            # Native close has completed before this path in the composed
            # owner. No accepted callback remains, so release every metadata
            # cell as well as the four workers.
            self._slots.clear()
        return self._lane.close(max(0.0, deadline - time.monotonic()))

    def snapshot(self) -> CallbackPoolSnapshot:
        with self._guard:
            return CallbackPoolSnapshot(
                len(self._slots), self._outstanding,
                sum(not slot.accepting for slot in self._slots.values()),
                self._workers, self._closing,
            )


DEFAULT_DECLARATION_CAPACITY = 1024
# Production E2E-021 owns 320 route Inbox subscribers plus roughly 15 startup
# observers. Cells are finite metadata; four workers handle their callbacks.
DEFAULT_CALLBACK_REGISTRATION_CAPACITY = 512
DEFAULT_CALLBACK_OUTSTANDING_CAPACITY = 1024
DEFAULT_CALLBACK_WORKERS = 4
# A cold liveliness subscription replays a route-sized history burst. Its
# default share covers the 320-route replay required by E2E-021.
# Keep half the existing total credit pool available to other registrations.
DEFAULT_CALLBACK_PER_TOKEN_CAPACITY = 512


class TransportSessionAuthority:
    def __init__(
        self, session, *, capacity=256, timeout=65.0,
        callback_capacity=DEFAULT_CALLBACK_PER_TOKEN_CAPACITY,
        declaration_capacity=DEFAULT_DECLARATION_CAPACITY,
        callback_registration_capacity=DEFAULT_CALLBACK_REGISTRATION_CAPACITY,
        callback_outstanding_capacity=DEFAULT_CALLBACK_OUTSTANDING_CAPACITY,
        callback_workers=DEFAULT_CALLBACK_WORKERS,
    ):
        if min(
            capacity, callback_capacity, declaration_capacity,
            callback_registration_capacity, callback_outstanding_capacity, callback_workers,
        ) < 1:
            raise ValueError("transport capacities must be positive")
        self._native = session
        self._config = session.config
        self._capacity = capacity
        self._callback_capacity = callback_capacity
        # A production route owns a liveliness token and an Inbox subscriber.
        # E2E-021's 320 routes consume 640 declaration cells and 320 callback
        # registration cells, plus startup registrations. Callback execution
        # stays on a fixed worker pool, independent of registration count.
        self._declaration_capacity = declaration_capacity
        self._callback_registration_capacity = callback_registration_capacity
        self._callback_outstanding_capacity = callback_outstanding_capacity
        self._callback_workers = callback_workers
        self._timeout = timeout
        self._guard = threading.Lock()
        self._close_guard = threading.Lock()
        self._registration_count = 0
        self._pending: dict[str, _Reply] = {}
        self._errors = {}
        self._callbacks = {}  # opaque fixed dependencies, not command values
        self._native_registrations = {}  # exclusive control-I/O ownership
        self._declarations: dict[str, Declare] = {}  # mailbox-owned intent
        self._hooks = set()
        self._deferred = deque()
        self._active = {}
        self._barrier = False
        self._generation = 1
        self._closing = False
        self._closed = False
        self._close_command = None
        self._rejected = self._stale = 0
        self._callbacks_complete = True
        self._generation_rejected = False
        self._native_overflow_seen = 0
        self._recover_after = 0.0
        self._recovery_scheduler = GenerationScheduler()
        self._runtime = ActorRuntime()
        handle = None
        data = None
        control = None
        callback_pool = None
        try:
            handle = self._runtime.start(
                ActorSpec(
                    name="transport-session",
                    handler_factory=lambda: self._receive,
                    mailbox_capacity=capacity,
                )
            )
            self._handle = handle
            data = EffectLane(
                name="transport-data",
                execute=self._execute,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=capacity,
                workers=4,
            )
            self._data = data
            control = EffectLane(
                name="transport-lifetime",
                execute=self._execute,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=1,
            )
            self._control = control
            callback_pool = _CallbackPool(
                self._invoke_callback, self._invalidate_callback,
                registrations=callback_registration_capacity,
                outstanding=callback_outstanding_capacity,
                per_token=callback_capacity,
                workers=callback_workers,
            )
            self._callback_pool = callback_pool
        except BaseException as error:
            # No constructor result exists for the application to close.
            # Effect lanes are still idle, so settle them before the actor.
            for resource, close in (
                ("callback pool", None if callback_pool is None else lambda: callback_pool.close(5.0)),
                ("control lane", None if control is None else control.close),
                ("data lane", None if data is None else data.close),
                (
                    "transport actor",
                    None if handle is None else lambda: self._runtime.stop(handle, 5.0),
                ),
                (
                    "recovery scheduler",
                    lambda: self._recovery_scheduler.shutdown(5.0),
                ),
            ):
                if close is None:
                    continue
                try:
                    if close() is False:
                        error.add_note(
                            f"{resource} did not drain during construction rollback"
                        )
                except BaseException as cleanup_error:
                    error.add_note(
                        f"{resource} rollback raised {type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise

    @property
    def config(self):
        with self._guard:
            return self._config

    def projection(self):
        read_overflow = getattr(self._native, "callback_overflow_count", None)
        native_overflow = read_overflow() if read_overflow is not None else 0
        with self._guard:
            changed = native_overflow != self._native_overflow_seen
            if changed:
                self._callbacks_complete = False
                self._generation_rejected = True
                self._rejected += max(0, native_overflow - self._native_overflow_seen)
                self._native_overflow_seen = native_overflow
            projection = SessionProjection(
                self._generation,
                len(self._pending),
                self._registration_count,
                self._rejected,
                self._stale,
                self._closed,
                self._callbacks_complete,
            )
        if changed:
            self._schedule_recovery()
        return projection

    def _admit(self, command, *, close_retry=False):
        reply = _Reply()
        with self._guard:
            if (self._closing and not close_retry) or len(
                self._pending
            ) >= self._capacity:
                self._rejected += 1
                raise RuntimeError("transport authority closed or overloaded")
            self._pending[command.operation_id] = reply
            result = self._runtime.tell(self._handle, command)
            if result is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                self._rejected += 1
                raise RuntimeError(f"transport admission {result.value}")
            if isinstance(command.payload, CloseSession):
                self._closing = True
                self._close_command = (command, reply)
        return reply

    def _wait(self, command, reply, timeout):
        if not reply.ready.wait(timeout):
            raise TimeoutError(
                f"transport operation {command.operation_id} remains accepted"
            )
        if reply.error is not None:
            raise reply.error
        return reply.result

    def _call(self, payload):
        command = SessionCommand(uuid4().hex, payload)
        return self._wait(command, self._admit(command), self._timeout)

    def _receive(self, event):
        if isinstance(event, SessionCommand):
            self._deferred.append(event)
        elif isinstance(event, RecoverCallbacks):
            pass
        elif isinstance(event, EffectCompleted):
            command = self._active.pop(event.operation_id, None)
            if command is None:
                return
            control = not isinstance(command.payload, (Publish, Query))
            lane = self._control if control else self._data
            lane.acknowledge(event.operation_id, event.generation)
            if control:
                self._barrier = False
            payload = command.payload
            if isinstance(payload, (Reconfigure, RefreshCallbacks)):
                with self._guard:
                    self._callbacks_complete = (
                        event.error is None and not self._generation_rejected
                    )
                if not self._callbacks_complete:
                    self._schedule_recovery()
            if event.error is None:
                if isinstance(payload, Declare):
                    self._declarations[payload.token] = payload
                elif isinstance(payload, Retire):
                    self._declarations.pop(payload.token, None)
                elif isinstance(payload, ChangeRebuildHook):
                    if payload.add:
                        self._hooks.add(payload.token)
                    else:
                        self._hooks.discard(payload.token)
                elif isinstance(payload, CloseSession):
                    with self._guard:
                        self._closed = True
            with self._guard:
                self._config = self._native.config
                self._registration_count = len(self._declarations)
                reply = self._pending.pop(event.operation_id)
                reply.result = event.result
                reply.error = self._errors.pop(event.operation_id, None)
                if event.error is not None and reply.error is None:
                    reply.error = RuntimeError(event.error)
                reply.ready.set()
        else:
            raise TypeError("unsupported transport command")
        self._pump()

    def _schedule_recovery(self):
        with self._guard:
            now = time.monotonic()
            if self._closing or self._recover_after > now:
                return
            self._recover_after = now + 1.0
            generation = self._generation
        self._recovery_scheduler.schedule(
            "callback-history",
            generation,
            1.0,
            lambda _generation: self._runtime.tell(self._handle, RecoverCallbacks()),
        )

    def _pump(self):
        if not self._deferred and not self._active and not self._barrier:
            with self._guard:
                if (
                    not self._closing
                    and not self._callbacks_complete
                    and time.monotonic() >= self._recover_after
                    and len(self._pending) < self._capacity
                ):
                    command = SessionCommand(uuid4().hex, RefreshCallbacks())
                    self._pending[command.operation_id] = _Reply()
                    self._deferred.append(command)
        while self._deferred and not self._barrier:
            command = self._deferred[0]
            control = not isinstance(command.payload, (Publish, Query))
            if control and self._active:
                return
            self._deferred.popleft()
            if isinstance(
                command.payload, (Reconfigure, RefreshCallbacks, CloseSession)
            ):
                with self._guard:
                    self._generation += 1
                    self._callbacks_complete = False
                    self._generation_rejected = False
            effect = SessionEffect(
                command,
                self._generation,
                tuple(self._declarations.values()),
                tuple(self._hooks),
            )
            lane = self._control if control else self._data
            admitted = lane.submit(
                EffectRequest(command.operation_id, self._generation, effect)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                # Total front-door custody bounds both queued and active work;
                # this is an invariant failure, not silently discarded work.
                with self._guard:
                    reply = self._pending.pop(command.operation_id)
                    reply.error = RuntimeError(f"transport effect {admitted.value}")
                    reply.ready.set()
                    self._rejected += 1
                continue
            self._active[command.operation_id] = command
            self._barrier = control

    def _current_callback(self, token, generation):
        with self._guard:
            if self._closed or generation != self._generation:
                self._stale += 1
                return None
            return self._callbacks.get(token)

    def _invoke_callback(self, token, event):
        callback = self._current_callback(token, event.generation)
        if callback is not None:
            try:
                callback(event.sample)
            except BaseException:
                # User/domain callback failures are concrete I/O-adapter
                # failures, not actor-runtime failures. Keep the worker
                # available so already-accepted later samples still settle,
                # invalidate its projection, and recover history under the
                # transport generation fence.
                self._invalidate_callback(token, event.generation)

    def _invalidate_callback(self, token, generation):
        with self._guard:
            if (
                self._closed
                or generation != self._generation
                or token not in self._callbacks
            ):
                return
            self._rejected += 1
            self._callbacks_complete = False
            self._generation_rejected = True
        self._schedule_recovery()

    def _declare_native(self, payload, generation):
        token, key = payload.token, payload.key
        if payload.kind in {"subscribe", "liveliness"}:

            def observed(sample):
                if self._current_callback(token, generation) is None:
                    return
                result = self._callback_pool.admit(
                    token,
                    CallbackObserved(
                        generation,
                        TransportSample(
                            str(sample.key),
                            bytes(sample.payload),
                            str(sample.kind),
                            generation,
                        ),
                    ),
                )
                if result is not AdmissionResult.ACCEPTED:
                    with self._guard:
                        # Retire/close can fence the pool after the first
                        # current-callback check. That late native invocation
                        # never transferred custody and needs no replay.
                        if (
                            self._closing
                            or self._closed
                            or generation != self._generation
                            or token not in self._callbacks
                        ):
                            return
                        self._rejected += 1
                        self._callbacks_complete = False
                        self._generation_rejected = True
                    self._schedule_recovery()

            if payload.kind == "subscribe":
                return self._native.subscribe(key, observed)
            return self._native.observe_liveliness(
                key, observed, history=payload.history
            )
        if payload.kind == "queryable":

            def answer(selector):
                callback = self._current_callback(token, generation)
                if callback is None:
                    return None
                result = callback(selector)
                return (
                    result
                    if self._current_callback(token, generation) is not None
                    else None
                )

            return self._native.declare_queryable(key, answer)
        if payload.kind == "query_handler":

            def answer_many(selector, value):
                callback = self._current_callback(token, generation)
                if callback is not None:
                    for item in callback(selector, value):
                        if self._current_callback(token, generation) is None:
                            return
                        yield item

            return self._native.declare_query_handler(key, answer_many)
        if payload.kind == "token":
            return self._native.declare_liveliness(key)
        raise TypeError("unsupported declaration kind")

    def _execute(self, effect):
        try:
            return self._operate(effect)
        except BaseException as error:
            with self._guard:
                self._errors[effect.command.operation_id] = error
            # EffectLane receipts carry only immutable failure codes.
            raise RuntimeError(type(error).__name__) from None

    def _operate(self, effect):
        payload = effect.command.payload
        if isinstance(payload, Publish):
            self._native.put(payload.key, payload.payload)
        elif isinstance(payload, Query):
            errors = []
            kwargs = dict(
                timeout=payload.timeout, errors=errors, all_replies=payload.all_replies
            )
            samples = (
                self._native.get(payload.key, **kwargs)
                if payload.payload is None
                else self._native.query(payload.key, payload.payload, **kwargs)
            )
            return QueryResult(tuple(samples), tuple(errors))
        elif isinstance(payload, Declare):
            self._native_registrations[payload.token] = self._declare_native(
                payload, effect.generation
            )
        elif isinstance(payload, Retire):
            registration = self._native_registrations.get(payload.token)
            if registration is not None:
                registration.close()
                self._native_registrations.pop(payload.token)
            with self._guard:
                self._callbacks.pop(payload.token, None)
            self._callback_pool.retire(payload.token)
        elif isinstance(payload, ChangeRebuildHook):
            if not payload.add:
                with self._guard:
                    self._callbacks.pop(payload.token, None)
        elif isinstance(payload, (Reconfigure, RefreshCallbacks)):
            error = None
            if isinstance(payload, Reconfigure):
                try:
                    self._native.reconfigure_connect(payload.endpoints)
                except BaseException as caught:
                    error = caught
            # The native adapter may roll back and replay its old closures.
            # Those closures retain the old generation and are ignored. Install
            # fresh callbacks even on rollback so current history can repopulate.
            for token in effect.hooks:
                callback = self._current_callback(token, effect.generation)
                if callback is not None:
                    callback()
            for declaration in effect.declarations:
                old = self._native_registrations.get(declaration.token)
                if old is not None:
                    old.close()
                self._native_registrations[declaration.token] = self._declare_native(
                    declaration, effect.generation
                )
            if error is not None:
                raise error
        elif isinstance(payload, CloseSession):
            self._native.close()
        else:
            raise TypeError("unsupported transport effect")

    def put(self, key, payload):
        return self._call(Publish(str(key), bytes(payload)))

    def get(self, key_expr, *, timeout=3.0, errors=None, all_replies=False):
        return self._query(Query(str(key_expr), None, timeout, all_replies), errors)

    def query(self, key_expr, payload, *, timeout=3.0, errors=None, all_replies=False):
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        return self._query(Query(str(key_expr), payload, timeout, all_replies), errors)

    def _query(self, command, errors):
        result = self._call(command)
        if errors is not None:
            errors.extend(result.errors)
        return list(result.samples)

    def callback_pool_snapshot(self) -> CallbackPoolSnapshot:
        return self._callback_pool.snapshot()

    def _declare(self, kind, key, callback=None, history=True):
        token = uuid4().hex
        needs_callback_slot = kind in {"subscribe", "liveliness"}
        with self._guard:
            if self._closing:
                raise RuntimeError("transport authority closed")
            retired = self._callback_pool.snapshot().retired
            if len(self._callbacks) + retired >= self._declaration_capacity:
                self._rejected += 1
                raise RuntimeError("transport declaration capacity exhausted")
            if needs_callback_slot:
                admitted = self._callback_pool.register(token)
                if admitted is not AdmissionResult.ACCEPTED:
                    self._rejected += 1
                    raise RuntimeError("transport callback registration capacity exhausted")
            self._callbacks[token] = callback
        try:
            self._call(Declare(token, kind, str(key), history))
        except TimeoutError:
            # An accepted declaration can still finish after caller timeout.
            # Keep its native and callback credits for close/retry custody.
            raise
        except BaseException:
            with self._guard:
                self._callbacks.pop(token, None)
            if needs_callback_slot:
                self._callback_pool.retire(token)
            raise
        return _RegistrationPort(self, token)

    def subscribe(self, key_expr, callback):
        return self._declare("subscribe", key_expr, callback)

    def observe_liveliness(self, key_expr, callback, *, history=True):
        return self._declare("liveliness", key_expr, callback, history)

    def declare_queryable(self, key_expr, handler):
        return self._declare("queryable", key_expr, handler)

    def declare_query_handler(self, key_expr, handler):
        return self._declare("query_handler", key_expr, handler)

    def declare_liveliness(self, key):
        return self._declare("token", key)

    def on_rebuild(self, hook):
        token = uuid4().hex
        with self._guard:
            if (
                self._closing
                or len(self._callbacks) + self._callback_pool.snapshot().retired
                >= self._declaration_capacity
            ):
                self._rejected += 1
                raise RuntimeError("transport hook capacity exhausted or closed")
            self._callbacks[token] = hook
        try:
            self._call(ChangeRebuildHook(token, True))
        except TimeoutError:
            raise
        except BaseException:
            with self._guard:
                self._callbacks.pop(token, None)
            raise
        return lambda: self._call(ChangeRebuildHook(token, False))

    def reconfigure_connect(self, endpoints):
        return self._call(Reconfigure(tuple(endpoints)))

    def lock_holder(self):
        read = getattr(self._native, "lock_holder", None)
        return read() if read is not None else None

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._close_guard:
            # Fence native RX at the caller edge. Keep the control actor and
            # both native-I/O lanes live while already-admitted callbacks finish:
            # a callback may be awaiting an I/O command admitted before close.
            with self._guard:
                self._closing = True
            self._callback_pool.close_admission()
            if not self._callback_pool.drain(
                max(0.0, deadline - time.monotonic())
            ):
                raise TimeoutError("transport callbacks still draining")
            with self._guard:
                existing = self._close_command
            failed_close = (
                existing is not None
                and existing[1].ready.is_set()
                and existing[1].error is not None
            )
            if existing is None or failed_close:
                command = SessionCommand(uuid4().hex, CloseSession())
                # Failed native close retains resource custody and can retry;
                # an in-flight close keeps its original completion cell.
                reply = self._admit(command, close_retry=True)
            else:
                command, reply = existing
        self._recovery_scheduler.shutdown(max(0.0, deadline - time.monotonic()))
        self._wait(command, reply, max(0.0, deadline - time.monotonic()))
        if not self._callback_pool.close(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("transport callback workers still draining")
        for lane in (self._data, self._control):
            if not lane.close(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("transport accepted effects still draining")
        if not self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic())):
            raise TimeoutError("transport authority still draining")
