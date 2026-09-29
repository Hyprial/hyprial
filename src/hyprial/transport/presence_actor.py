"""Mailbox-owned presence with immutable, generation-fenced projections."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceChanged:
    generation: int
    kind: str
    identity: str
    present: bool


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
    def __init__(self, *, capacity: int = 4096):
        self._guard = threading.Lock()
        self._generation = 1
        self._owner_generation = 1
        self._closed = False
        self._online_observer: Callable[[ActorOnlineTransition], object] | None = None
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

    def change(
        self, kind: str, identity: str, present: bool, *, generation: int | None = None
    ) -> AdmissionResult:
        if kind not in {"actor", "mailbox"}:
            raise ValueError("invalid presence kind")
        with self._guard:
            result = (
                AdmissionResult.CLOSED
                if self._closed
                else self._runtime.tell(
                    self._handle,
                    PresenceChanged(
                        self._generation if generation is None else generation,
                        kind,
                        identity,
                        present,
                    ),
                )
            )
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
            self._online_sequence = 0
            self._projection = PresenceProjection(
                self._generation, rejected=self._projection.rejected
            )

    def _receive(self, command: object) -> None:
        if not isinstance(command, PresenceChanged):
            raise TypeError("unsupported presence command")
        transition: ActorOnlineTransition | None = None
        observer: Callable[[ActorOnlineTransition], object] | None = None
        with self._guard:
            if command.generation != self._generation:
                return
            if self._owner_generation != command.generation:
                self._actors.clear()
                self._mailboxes.clear()
                self._owner_generation = command.generation
                self._online_sequence = 0
            values = self._actors if command.kind == "actor" else self._mailboxes
            newly_online = command.present and command.identity not in values
            if command.present:
                values.add(command.identity)
            else:
                values.discard(command.identity)
            if self._projection.complete:
                if command.kind == "actor" and newly_online:
                    self._online_sequence += 1
                self._projection = PresenceProjection(
                    command.generation,
                    frozenset(self._actors),
                    frozenset(self._mailboxes),
                    rejected=self._projection.rejected,
                    online_sequence=self._online_sequence,
                )
                if command.kind == "actor" and newly_online:
                    transition = ActorOnlineTransition(
                        command.generation,
                        command.identity,
                        self._online_sequence,
                    )
                    observer = self._online_observer
        if transition is not None and observer is not None:
            self._notify_online(observer, transition)

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

    def close(self, timeout: float = 1.0) -> bool:
        with self._guard:
            self._closed = True
            self._online_observer = None
            self._generation += 1
            self._projection = PresenceProjection(
                self._generation, complete=False, rejected=self._projection.rejected
            )
        return self._runtime.stop(self._handle, timeout)
