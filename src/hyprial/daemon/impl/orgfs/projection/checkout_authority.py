"""One bounded checkout I/O owner per materialized orgfs space."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import uuid
import weakref
from pathlib import Path
from typing import Any

from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.orgfs.api  import ChangeEvent, OrgFsError
from hyprial.daemon.impl.orgfs.projection.checkout  import CheckoutManager

_CHECKOUT_CAPACITY = 32


@dataclass(frozen=True, slots=True)
class _CheckoutCommand:
    operation_id: str
    generation: int
    action: str
    kind: str | None = None
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CheckoutIo:
    operation: str
    kind: str | None = None
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CheckoutResult:
    error: str | None = None


@dataclass(slots=True)
class _CheckoutWaiter:
    done: threading.Event
    error: str | None = None


class CheckoutAuthority:
    """Bounded state mailbox plus a dedicated serial filesystem lane per space.

    Facade watchers and directory liveliness only admit immutable commands.
    Reconciliation may read facade projections, but those paths never wait for
    this actor, so the checkout-to-facade edge cannot form a reverse wait cycle.
    """

    def __init__(
        self, facade: Any, blobs: Any, space_id: str, root: Path | str
    ) -> None:
        self.space_id = space_id
        self.manager = CheckoutManager(facade, blobs, space_id, root)
        self._facade = facade
        self._lock = threading.RLock()
        self._generation = 1
        self._closing = False
        self._closed = False
        self._enabled = False
        self._dirty = False
        self._disable_pending = False
        self._watch = None
        self._waiters: dict[str, _CheckoutWaiter] = {}
        self._waiter_batches: list[str] = []
        self._stop_waiters: list[str] = []
        self._commands: dict[str, _CheckoutCommand] = {}
        self._submitted: set[str] = set()
        self._active: tuple[str, int, tuple[str, ...]] | None = None
        self._ingress_dropped = 0
        owner_ref = weakref.ref(self)

        def actor_event(event: ActorEvent) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_actor_event(event)

        def handle(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_command(command)

        def execute(request: _CheckoutIo) -> _CheckoutResult:
            owner = owner_ref()
            if owner is None:
                return _CheckoutResult("checkout authority was collected")
            try:
                if request.operation == "reconcile":
                    # Membership/read authority is checked inside the per-space
                    # I/O owner along with the projection reads, never on the
                    # directory lifecycle or watcher admission path.
                    owner.manager.facade.stat(owner.space_id, "id:root")
                    owner.manager.reconcile()
                elif request.operation == "change":
                    owner.manager.apply_change(request.kind or "changed", request.node_id or "")
                elif request.operation == "disable":
                    owner.manager.disable()
                else:
                    raise ValueError(f"unknown checkout I/O operation {request.operation}")
                return _CheckoutResult()
            except Exception as error:
                return _CheckoutResult(f"{type(error).__name__}: {error}")

        self._runtime = ActorRuntime(event_sink=actor_event)
        self._actor: ActorHandle = self._runtime.start(
            ActorSpec(
                f"orgfs-checkout-{space_id[:12]}",
                lambda: handle,
                mailbox_capacity=_CHECKOUT_CAPACITY,
                supervision_profile="state_authority",
            )
        )

        def complete(completion: EffectCompleted[_CheckoutResult]) -> AdmissionResult:
            owner = owner_ref()
            if owner is None:
                return AdmissionResult.CLOSED
            return owner._runtime.tell(owner._actor, completion)

        self._effects: EffectLane[_CheckoutIo, _CheckoutResult] = EffectLane(
            name=f"orgfs-checkout-{space_id[:12]}-io",
            execute=execute,
            complete=complete,
            capacity=_CHECKOUT_CAPACITY,
            workers=1,
        )

    @property
    def ingress_dropped(self) -> int:
        with self._lock:
            return self._ingress_dropped

    def _tell(self, command: _CheckoutCommand) -> AdmissionResult:
        with self._lock:
            if self._closed or self._closing:
                return AdmissionResult.CLOSED
            self._commands[command.operation_id] = command
        admission = self._runtime.tell(self._actor, command)
        if admission is not AdmissionResult.ACCEPTED:
            with self._lock:
                self._commands.pop(command.operation_id, None)
        return admission

    def _ask(
        self,
        action: str,
        *,
        allow_closing: bool = False,
        timeout: float | None = None,
    ) -> None:
        operation_id = uuid.uuid4().hex
        waiter = _CheckoutWaiter(threading.Event())
        with self._lock:
            if self._closed or (self._closing and not allow_closing):
                raise OrgFsError("unavailable", {"message": "checkout authority is closing"})
            if len(self._waiters) >= _CHECKOUT_CAPACITY:
                raise OrgFsError("resource-exhausted", {"message": "checkout waiters are full"})
            self._waiters[operation_id] = waiter
            command = _CheckoutCommand(operation_id, self._generation, action)
            self._commands[operation_id] = command
        while True:
            admission = self._runtime.tell(self._actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = self._commands.get(operation_id)
                if admission is AdmissionResult.CLOSED and current is not None and current.generation != command.generation:
                    command = current
                    continue
                self._waiters.pop(operation_id, None)
                self._commands.pop(operation_id, None)
            raise OrgFsError(
                "resource-exhausted" if admission is AdmissionResult.OVERLOADED else "unavailable",
                {"message": "checkout command admission was " + admission.value},
            )
        if not waiter.done.wait(timeout):
            # Reporting a stopped wait does not revoke an admitted filesystem
            # operation. The operation remains in custody and close may retry.
            with self._lock:
                self._waiters.pop(operation_id, None)
            raise TimeoutError("checkout operation remains in progress")
        if waiter.error is not None:
            raise OrgFsError("checkout-io", {"message": waiter.error})

    def enable(self) -> None:
        self._ask("enable")

    def disable(self) -> None:
        self._ask("disable")

    def reconcile(self) -> None:
        self._ask("reconcile")

    def request_reconcile(self) -> None:
        command = _CheckoutCommand(uuid.uuid4().hex, self._generation, "reconcile")
        if self._tell(command) is not AdmissionResult.ACCEPTED:
            with self._lock:
                self._ingress_dropped += 1

    def notify_change(self, event: ChangeEvent) -> None:
        command = _CheckoutCommand(
            uuid.uuid4().hex,
            self._generation,
            "change",
            str(event.kind),
            str(event.node.node_id),
        )
        if self._tell(command) is not AdmissionResult.ACCEPTED:
            with self._lock:
                self._ingress_dropped += 1

    def _on_actor_event(self, event: ActorEvent) -> None:
        if (
            event.handle.name != f"orgfs-checkout-{self.space_id[:12]}"
            or event.kind is not ActorEventKind.CHILD_RESTARTED
        ):
            return
        with self._lock:
            if event.generation <= self._generation:
                return
            self._generation = event.generation
            replay = tuple(
                _CheckoutCommand(
                    command.operation_id,
                    event.generation,
                    command.action,
                    command.kind,
                    command.node_id,
                )
                for operation_id, command in self._commands.items()
                if operation_id not in self._submitted
            )
            for command in replay:
                self._commands[command.operation_id] = command
        for command in replay:
            if self._runtime.tell(self._actor, command) is AdmissionResult.CLOSED:
                return

    def _on_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            self._effects.acknowledge(command.operation_id, command.generation)
            with self._lock:
                active = self._active
                if active is None or active[0] != command.operation_id:
                    return
                _, _, waiter_ids = active
                self._active = None
                self._submitted.discard(command.operation_id)
                self._commands.pop(command.operation_id, None)
                error = (
                    command.result.error
                    if command.result is not None
                    else command.error or "checkout effect failed"
                )
                for waiter_id in waiter_ids:
                    waiter = self._waiters.pop(waiter_id, None)
                    if waiter is not None:
                        waiter.error = error
                        waiter.done.set()
                stop_waiters = tuple(self._stop_waiters) if self._active is None else ()
                if stop_waiters and not self._disable_pending:
                    self._stop_waiters.clear()
                    for waiter_id in stop_waiters:
                        self._complete_waiter(waiter_id, None)
            self._schedule_next()
            return
        if not isinstance(command, _CheckoutCommand):
            raise TypeError("checkout owner received an invalid command")
        close_watch = None
        open_watch = False
        with self._lock:
            if (
                command.generation != self._generation
                or self._closed
                or (self._closing and command.action not in {"disable", "stop"})
            ):
                self._complete_waiter(command.operation_id, "checkout generation is stale")
                return
            self._commands.pop(command.operation_id, None)
            if command.action == "enable":
                self._enabled = True
                open_watch = self._watch is None
                self._dirty = True
                self._attach_waiter(command.operation_id)
            elif command.action == "disable":
                self._enabled = False
                self._generation += 1
                watch, self._watch = self._watch, None
                close_watch = watch
                self._disable_pending = True
                self._attach_waiter(command.operation_id)
            elif command.action == "stop":
                self._enabled = False
                self._generation += 1
                watch, self._watch = self._watch, None
                close_watch = watch
                self._dirty = False
                if self._active is None:
                    self._complete_waiter(command.operation_id, None)
                else:
                    self._stop_waiters.append(command.operation_id)
            elif command.action == "reconcile":
                if self._enabled:
                    self._dirty = True
                    self._attach_waiter(command.operation_id)
                else:
                    self._complete_waiter(command.operation_id, None)
            elif command.action == "change":
                if self._enabled:
                    self._dirty = True
            else:
                self._complete_waiter(command.operation_id, f"unknown checkout command {command.action}")
        if close_watch is not None:
            close_watch.close()
        if open_watch:
            try:
                watch = self._facade.watch(self.space_id, "*", self.notify_change)
            except Exception as error:
                with self._lock:
                    self._enabled = False
                    self._dirty = False
                    self._complete_waiter(
                        command.operation_id,
                        f"{type(error).__name__}: {error}",
                    )
                return
            with self._lock:
                if self._enabled and not self._closing and not self._closed:
                    self._watch = watch
                else:
                    close_watch = watch
            if close_watch is not None:
                close_watch.close()
        self._schedule_next()

    def _attach_waiter(self, operation_id: str) -> None:
        if operation_id in self._waiters:
            self._waiter_batches.append(operation_id)

    def _complete_waiter(self, operation_id: str, error: str | None) -> None:
        waiter = self._waiters.pop(operation_id, None)
        if waiter is not None:
            waiter.error = error
            waiter.done.set()

    def _schedule_next(self) -> None:
        with self._lock:
            if self._active is not None:
                return
            if self._disable_pending:
                operation = "disable"
                self._disable_pending = False
                self._dirty = False
            elif self._dirty and self._enabled:
                operation = "reconcile"
                self._dirty = False
            else:
                return
            operation_id = uuid.uuid4().hex
            generation = self._generation
            waiter_ids = tuple(self._waiter_batches)
            self._waiter_batches.clear()
            request = _CheckoutIo(operation)
            self._active = (operation_id, generation, waiter_ids)
            self._submitted.add(operation_id)
        admission = self._effects.submit(EffectRequest(operation_id, generation, request))
        if admission is not AdmissionResult.ACCEPTED:
            with self._lock:
                self._active = None
                self._submitted.discard(operation_id)
                self._dirty |= operation == "reconcile" and self._enabled
                self._disable_pending |= operation == "disable"
                for waiter_id in waiter_ids:
                    self._complete_waiter(waiter_id, "checkout effect lane is " + admission.value)
            return

    def close(self, timeout: float = 5.0, *, remove_tree: bool = False) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._closing = True
        # Stop detaches the watcher and drains accepted writes while preserving
        # the materialized tree for the next daemon generation.
        try:
            self._ask(
                "disable" if remove_tree else "stop",
                allow_closing=True,
                timeout=timeout,
            )
        except OrgFsError:
            pass
        except TimeoutError:
            return False
        with self._lock:
            self._closing = True
            self._closed = True
        drained = self._effects.close(timeout=timeout)
        self._runtime.stop(self._actor, timeout=timeout)
        return drained
