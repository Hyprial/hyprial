"""Mailbox-owned presence with immutable, generation-fenced projections."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceChanged:
    generation: int
    kind: str
    identity: str
    present: bool
    revision: int = 0
    deadline: float | None = None
    refresh: bool = False


@dataclass(frozen=True, slots=True)
class PresenceProjection:
    generation: int
    actors: frozenset[str] = frozenset()
    mailboxes: frozenset[str] = frozenset()
    complete: bool = True
    rejected: int = 0
    online_sequence: int = 0


@dataclass(frozen=True, slots=True)
class ActorOnlineTransition:
    generation: int
    actor: str
    sequence: int = 0


class PresenceAuthority:
    def __init__(
        self, *, capacity: int = 4096,
        confirm_present: Callable[[str, str, float], bool] | None = None,
        reconcile_timeout: float = 1.0,
    ):
        if not math.isfinite(reconcile_timeout) or reconcile_timeout <= 0:
            raise ValueError("presence reconcile timeout must be positive")
        self._guard = threading.Lock()
        # Retain the original mailbox bound plus its one executing command.
        # Query custody consumes this SAME admission allowance until settlement.
        self._custody_limit = capacity + 1
        self._accepted: set[int] = set()
        self._revision = 0
        self._latest: dict[tuple[str, str], PresenceChanged] = {}
        self._probes: dict[str, PresenceChanged] = {}
        self._confirm_present = confirm_present
        self._reconcile_timeout = reconcile_timeout
        self._delete_effects: EffectLane[PresenceChanged, bool | None] | None = None
        self._stop_when_drained = False
        self._stop_requested = False
        self._generation = 1
        self._owner_generation = 1
        self._closed = False
        self._online_observer: Callable[[ActorOnlineTransition], object] | None = None
        self._online_subscribers: dict[object, Callable[[ActorOnlineTransition], object]] = {}
        self._online_sequence = 0
        self._actors: set[str] = set()
        self._mailboxes: set[str] = set()
        self._projection = PresenceProjection(1)
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="presence",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        if confirm_present is not None:
            try:
                self._delete_effects = EffectLane(
                    name="presence-delete-reconcile", execute=self._probe,
                    complete=lambda result: self._runtime.tell(self._handle, result),
                    capacity=capacity, workers=1,
                )
            except BaseException as error:
                if not self._runtime.stop(self._handle, timeout=1.0):
                    error.add_note("presence mailbox construction rollback did not drain")
                raise

    def change(
        self, kind: str, identity: str, present: bool, *, generation: int | None = None
    ) -> AdmissionResult:
        if kind not in {"actor", "mailbox"}:
            raise ValueError("invalid presence kind")
        with self._guard:
            current_generation = self._generation if generation is None else generation
            if not self._closed and current_generation != self._generation:
                return AdmissionResult.ACCEPTED  # already fenced, no new custody
            self._revision += 1
            previous = self._latest.get((kind, identity))
            command = PresenceChanged(
                current_generation, kind, identity, present, self._revision,
                time.monotonic() + self._reconcile_timeout if not present else None,
                # A PUT superseding a held DELETE is fresh online evidence,
                # even though the old online projection stayed up during I/O.
                refresh=bool(present and previous is not None and not previous.present),
            )
            if self._closed:
                result = AdmissionResult.CLOSED
            elif len(self._accepted) >= self._custody_limit:
                result = AdmissionResult.OVERLOADED
            else:
                result = self._runtime.tell(self._handle, command)
            if result is AdmissionResult.ACCEPTED:
                self._accepted.add(command.revision)
                self._latest[(kind, identity)] = command
            if result is not AdmissionResult.ACCEPTED:
                # A lost delete must never leave a phantom online actor. Until
                # history is replayed, this generation's projection is unknown.
                self._projection = PresenceProjection(
                    self._generation,
                    complete=False,
                    rejected=self._projection.rejected + 1,
                )
            return result

    def reset(self) -> None:
        with self._guard:
            self._generation += 1
            self._latest.clear()
            self._online_sequence = 0
            self._projection = PresenceProjection(
                self._generation, rejected=self._projection.rejected
            )

    def _receive(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            lane = self._delete_effects
            with self._guard:
                pending = self._probes.get(command.operation_id)
                if pending is None or command.generation != pending.generation:
                    return
                self._probes.pop(command.operation_id)
                current = self._is_current(pending)
                transition, observers = (None, ())
                if current:
                    present = command.error is None and command.result is True
                    transition, observers = self._apply(pending, present, confirmed=present)
                    if command.error is not None:
                        logger.warning("presence.delete.reconcile_failed: %s", command.error)
                self._release(pending)
            if lane is None:
                raise RuntimeError("presence reconcile completion has no effect owner")
            lane.acknowledge(command.operation_id, command.generation)
            if transition is not None:
                for observer in observers:
                    self._notify_online(observer, transition)
            self._request_stop_if_drained()
            return
        if not isinstance(command, PresenceChanged):
            raise TypeError("unsupported presence command")
        transition: ActorOnlineTransition | None = None
        observers: tuple[Callable[[ActorOnlineTransition], object], ...] = ()
        with self._guard:
            if self._closed or command.generation != self._generation:
                self._release(command)
                return
            if not command.present and not self._is_current(command):
                self._release(command)
                return
            if self._owner_generation != command.generation:
                self._actors.clear()
                self._mailboxes.clear()
                self._owner_generation = command.generation
                self._online_sequence = 0
            lane = self._delete_effects
            if not command.present and lane is not None:
                operation_id = str(command.revision)
                self._probes[operation_id] = command
                result = lane.submit(EffectRequest(operation_id, command.generation, command))
                if result is AdmissionResult.ACCEPTED:
                    return
                self._probes.pop(operation_id)
                logger.warning("presence.delete.reconcile_refused: %s", result.value)
                # No extra retry/queue: a refused check accepts offline.
            transition, observers = self._apply(command, command.present, confirmed=command.refresh)
            self._release(command)
        if transition is not None:
            for observer in observers:
                self._notify_online(observer, transition)

    def observe_actor_online(
        self, observer: Callable[[ActorOnlineTransition], object]
    ) -> Callable[[], None]:
        """Add a bounded, independently retired observer of committed presence.

        The legacy binding remains a single replaceable slot. OrgFS must not
        replace the inbox's recipient-wake binding (or be replaced by it).
        Observers only admit work; they must not perform I/O in this mailbox.
        """
        token = object()
        with self._guard:
            if self._closed:
                raise RuntimeError("presence authority is closed")
            if len(self._online_subscribers) >= 16:
                raise RuntimeError("presence observer capacity exhausted")
            self._online_subscribers[token] = observer
            projection = self._projection
            replay = tuple(sorted(projection.actors)) if projection.complete else ()
        for actor in replay:
            # A concurrent commit can also notify this subscriber: replay is
            # at-least-once, so consumers must tolerate duplicate online wakes.
            self._notify_online(observer, ActorOnlineTransition(
                projection.generation, actor, projection.online_sequence,
            ))

        def stop() -> None:
            with self._guard:
                self._online_subscribers.pop(token, None)

        return stop

    def _is_current(self, command: PresenceChanged) -> bool:
        latest = self._latest.get((command.kind, command.identity))
        return (
            not self._closed and command.generation == self._generation
            and latest is not None and latest.revision == command.revision
        )

    def _release(self, command: PresenceChanged) -> None:
        self._accepted.discard(command.revision)
        key = (command.kind, command.identity)
        latest = self._latest.get(key)
        if latest is not None and latest.revision == command.revision:
            self._latest.pop(key)

    def _probe(self, command: PresenceChanged) -> bool | None:
        with self._guard:
            if not self._is_current(command):
                return None
        deadline = command.deadline
        if deadline is None or time.monotonic() >= deadline:
            raise TimeoutError("presence reconcile deadline expired before query")
        confirm = self._confirm_present
        if confirm is None:
            raise RuntimeError("presence query dependency is absent")
        present = confirm(command.kind, command.identity, deadline - time.monotonic())
        if time.monotonic() > deadline:
            raise TimeoutError("presence reconcile deadline expired during query")
        return present is True

    def _apply(self, command: PresenceChanged, present: bool, *, confirmed: bool = False):
        values = self._actors if command.kind == "actor" else self._mailboxes
        newly_online = present and (command.identity not in values or confirmed)
        if present:
            values.add(command.identity)
        else:
            values.discard(command.identity)
        if not self._projection.complete:
            return None, ()
        if command.kind == "actor" and newly_online:
            self._online_sequence += 1
        self._projection = PresenceProjection(
            command.generation, frozenset(self._actors), frozenset(self._mailboxes),
            rejected=self._projection.rejected, online_sequence=self._online_sequence,
        )
        if command.kind == "actor" and newly_online:
            # Both PUT and query-confirmed DELETE settle through this method.
            # Snapshot every sink under the projection lock; callers emit after
            # releasing it, preserving the inbox slot and independent OrgFS wake.
            observers = tuple(self._online_subscribers.values())
            if self._online_observer is not None:
                observers = (self._online_observer, *observers)
            return ActorOnlineTransition(
                command.generation, command.identity, self._online_sequence,
            ), observers
        return None, ()

    def bind_actor_online(
        self, observer: Callable[[ActorOnlineTransition], object]
    ) -> Callable[[], None]:
        """Install one observer and replay the committed online projection."""

        with self._guard:
            if self._closed:
                raise RuntimeError("presence authority is closed")
            self._online_observer = observer
            projection = self._projection
            replay = tuple(sorted(projection.actors)) if projection.complete else ()
        for actor in replay:
            self._notify_online(
                observer,
                ActorOnlineTransition(
                    projection.generation,
                    actor,
                    projection.online_sequence,
                ),
            )

        def stop() -> None:
            with self._guard:
                if self._online_observer is observer:
                    self._online_observer = None

        return stop

    @staticmethod
    def _notify_online(
        observer: Callable[[ActorOnlineTransition], object],
        transition: ActorOnlineTransition,
    ) -> None:
        try:
            observer(transition)
        except Exception:
            logger.exception("actor-online observer failed")

    def projection(self) -> PresenceProjection:
        with self._guard:
            return self._projection

    def close_admission(self) -> None:
        """Fence new observations before unregistering transport callbacks."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
            self._online_observer = None
            self._online_subscribers.clear()
            self._generation += 1
            self._latest.clear()
            self._projection = PresenceProjection(
                self._generation, complete=False, rejected=self._projection.rejected
            )

    def close(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        self.close_admission()
        if self._delete_effects is not None:
            if not self._delete_effects.close(max(0.0, deadline - time.monotonic())):
                self.stop_after_drain()
                return False  # retain I/O custody, then stop without a caller retry
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))

    def stop_after_drain(self) -> None:
        """Retain cleanup ownership after construction or close cannot finish."""
        self.close_admission()
        with self._guard:
            self._stop_when_drained = True
        if self._delete_effects is not None:
            self._delete_effects.close(0)
        self._request_stop_if_drained()

    def _request_stop_if_drained(self) -> None:
        with self._guard:
            if not self._stop_when_drained or self._stop_requested:
                return
            if self._delete_effects is not None and self._delete_effects.snapshot().outstanding:
                return
            self._stop_requested = True
        # All effect receipts are acknowledged before this nonblocking stop,
        # including when it is requested by our final completion handler.
        self._runtime.stop(self._handle, timeout=0)
