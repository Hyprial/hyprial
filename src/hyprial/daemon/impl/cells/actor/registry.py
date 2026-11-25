"""The keyed cell actor registry defined by the v8 cell actor contract.

Design decision, I5-1, 2026-11-20: the registry owns only ``closed``, the
current index ids, active ids and diagnostic de-duplication ids.  Engine
lifecycle state is read from the engine, input is read from CellRuntime, and
history and snapshots are owned by their stores.  The short registry lock is
never held across an engine call, runtime read, store operation, callback or
log call.  Every engine command is a content-free wakeup.  An incarnation gets
an epoch only while handling its first wakeup, after its factory has already
been constructed.

The public entry is named ``ensure_actor`` rather than ``ensure`` because the
network-reachability analysis resolves method short names.  It abandons
fallback when more than eight candidates share a short name, which would
disconnect an unrelated existing path from the static graph.

Design decision, cell model I5-2 (2026-11-20): the degraded copy a
quarantined actor's snapshot read returns also drops ``entry_member`` to
None.  The copy's answer is "start no new work", and a quarantined
incarnation's last effective structure is not an entry a consumer may act
on; the mapping of the other snapshot values keeps the I5-1 contract's E9
rule unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.cells.actor.actor import (
    CellActorHandler,
)
from hyprial.daemon.impl.cells.actor.contracts import (
    CellActorInput,
    CellActorInputPort,
    CellActorState,
    CellActorWakeup,
    CellCommandExtension,
)
from hyprial.daemon.impl.cells.actor.snapshot import (
    CellActorHistoryStore,
    CellActorSnapshot,
    CellActorSnapshotStore,
)
from hyprial.kernel import (
    STATE_AUTHORITY,
    ActorCell,
    ActorEvent,
    ActorEventKind,
    ActorSpawnRefused,
    ActorSpec,
    ActorState,
    AdmissionResult,
)

if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.ports import CellRuntimePort
    from hyprial.kernel import ActorHost

MAILBOX_CAPACITY = 128
STOP_TIMEOUT_S = 1.0
_CELL_ACTOR_GROUP = "cell"


class _RuntimeInputPort:
    __slots__ = ("_lookup", "_health")

    def __init__(
        self,
        lookup: Callable[[str], Any],
        health: Callable[[], Any],
        cell_id: str,
    ) -> None:
        self._lookup = lambda: lookup(cell_id)
        self._health = health

    def read(self) -> CellActorInput:
        cell = self._lookup()
        health = self._health()
        value = (health.source_stale, health.detail)
        return CellActorInput(
            document=cell,
            document_version=0 if cell is None else cell.revision,
            projection=None,
            health=value,
            health_version=health.updated_at_ms,
        )


class CellActorRegistry:
    """The sole creator and stopper of actors in the ``cell`` key group."""

    def __init__(
        self,
        host: ActorHost,
        *,
        runtime: CellRuntimePort | None = None,
        input_port_factory: Callable[[str], CellActorInputPort] | None = None,
        clock_ms: Callable[[], int] | None = None,
        logger: Callable[..., None] | None = None,
        extensions: Iterable[CellCommandExtension] = (),
        history: CellActorHistoryStore | None = None,
        snapshots: CellActorSnapshotStore | None = None,
        request_index_retry: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._runtime = runtime
        self._input_port_factory = input_port_factory
        self._clock = clock_ms
        self._logger = logger
        self._history = history or CellActorHistoryStore()
        self._snapshots = snapshots or CellActorSnapshotStore()
        self._extensions = tuple(extensions)
        self._request_index_retry = request_index_retry
        if runtime is not None and request_index_retry is None:
            self._request_index_retry = getattr(runtime, "request_index_retry", None)
        self._lock = threading.Lock()
        self._closed = False
        self._current: set[str] = set()
        self._active: set[str] = set()
        self._reported: set[str] = set()

    @property
    def group(self) -> str:
        return _CELL_ACTOR_GROUP

    def on_index(self, cell_ids: Iterable[str]) -> None:
        ids = self._coerce_ids(cell_ids)
        with self._lock:
            if self._closed:
                return
            previous = set(self._current)
            self._current = ids
            removed = self._active - ids
            self._active.difference_update(removed)
            self._reported.difference_update(previous - ids)
        for cell_id in sorted(ids):
            try:
                self.ensure_actor(cell_id)
            except Exception as error:
                self._log("warn", "cell.actor_index_failed", cell=cell_id,
                          errorType=type(error).__name__)
        for cell_id in sorted(removed):
            try:
                actor = self._find(cell_id)
                if actor is not None:
                    self._stop(actor, cell_id)
            except Exception as error:
                self._log("warn", "cell.actor_remove_failed", cell=cell_id,
                          errorType=type(error).__name__)

    def on_health(self) -> None:
        with self._lock:
            if self._closed:
                return
            active = tuple(self._active)
        for cell_id in active:
            try:
                actor = self._find(cell_id)
                if actor is None:
                    self._retry(cell_id, "missing")
                    continue
                self._process_handle(cell_id, actor)
            except Exception as error:
                self._log("warn", "cell.actor_health_failed", cell=cell_id,
                          errorType=type(error).__name__)

    def notify_health(self, *_args: Any, **_kwargs: Any) -> int:
        with self._lock:
            count = len(self._active)
        self.on_health()
        return count

    def ensure_actor(self, cell_id: str) -> ActorCell | None:
        with self._lock:
            if self._closed or cell_id not in self._current:
                return None
        actor = self._find(cell_id)
        if actor is None:
            actor = self._spawn(cell_id)
            if actor is None:
                return None
        return self._process_handle(cell_id, actor)

    def snapshot(self, cell_id: str) -> CellActorSnapshot | None:
        with self._lock:
            if self._closed:
                return None
        value = self._snapshots.get(cell_id)
        actor = self._find(cell_id)
        if value is None or actor is None:
            return None
        try:
            state = actor.snapshot().state
        except Exception:
            return None
        if state in {ActorState.STOPPING, ActorState.STOPPED}:
            return None
        if state is ActorState.QUARANTINED:
            value = replace(
                value,
                state=CellActorState.DEGRADED,
                reason="actor-quarantined",
                entry_member=None,
            )
        with self._lock:
            if self._closed:
                return None
        return value

    def snapshots(self) -> dict[str, CellActorSnapshot]:
        return dict(self._snapshots.snapshots())

    def detach(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = tuple(self._active)
            self._current.clear()
            self._active.clear()
            self._reported.clear()
        for cell_id in handles:
            try:
                actor = self._find(cell_id)
                if actor is not None:
                    self._stop(actor, cell_id)
            except Exception as error:
                self._log("warn", "cell.actor_detach_failed", cell=cell_id,
                          errorType=type(error).__name__)

    def _spawn(self, cell_id: str) -> ActorCell | None:
        spec = ActorSpec(
            name=f"{_CELL_ACTOR_GROUP}-{cell_id}",
            handler_factory=self._factory(cell_id),
            mailbox_capacity=MAILBOX_CAPACITY,
            supervision_profile=STATE_AUTHORITY,
            event_sink=self._event_sink(cell_id),
            group=_CELL_ACTOR_GROUP,
            key=cell_id,
        )
        try:
            return self._host.spawn(spec)
        except ActorSpawnRefused as error:
            if error.reason == "key-in-use":
                actor = self._find(cell_id)
                if actor is not None:
                    return actor
                self._diagnose(cell_id, "cell.actor_key_in_use", reason=str(error))
                self._retry(cell_id, "key-in-use")
                return None
            self._log("warn", "cell.actor_spawn_refused", cell=cell_id,
                      reason=str(error)[:200])
            return None
        except Exception as error:
            self._log("warn", "cell.actor_spawn_failed", cell=cell_id,
                      errorType=type(error).__name__, detail=str(error)[:200])
            return None

    def _process_handle(
        self, cell_id: str, actor: ActorCell
    ) -> ActorCell | None:
        try:
            state = actor.snapshot().state
        except Exception as error:
            self._log("warn", "cell.actor_snapshot_failed", cell=cell_id,
                      errorType=type(error).__name__)
            return None
        if state in {ActorState.RUNNING, ActorState.RESTART_PENDING}:
            try:
                result = actor.tell(CellActorWakeup())
            except Exception as error:
                self._log("warn", "cell.actor_tell_failed", cell=cell_id,
                          errorType=type(error).__name__)
                return None
            if result is AdmissionResult.CLOSED:
                self._retry(cell_id, "closed")
                return None
            if result not in {AdmissionResult.ACCEPTED, AdmissionResult.OVERLOADED}:
                return None
            if not self._mark_active(cell_id, actor, clear_reported=True):
                return None
            return actor
        if state is ActorState.STOPPING:
            self._diagnose(cell_id, "cell.actor_stopping")
            self._retry(cell_id, "stopping")
        elif state is ActorState.STOPPED:
            self._retry(cell_id, "stopped")
        elif state is ActorState.QUARANTINED:
            self._diagnose(cell_id, "cell.actor_quarantined")
        self._mark_active(cell_id, actor, clear_reported=False)
        return None

    def _mark_active(
        self, cell_id: str, actor: ActorCell, *, clear_reported: bool
    ) -> bool:
        with self._lock:
            if self._closed or cell_id not in self._current:
                stale = True
            else:
                self._active.add(cell_id)
                if clear_reported:
                    self._reported.discard(cell_id)
                stale = False
        if stale:
            self._stop(actor, cell_id)
        return not stale

    def _factory(self, cell_id: str) -> Callable[[], CellActorHandler]:
        def factory() -> CellActorHandler:
            projection = self._history.read(cell_id)
            return CellActorHandler(
                cell_id=cell_id,
                projection=projection,
                clock_ms=self._clock or _wall_ms,
                input_port=self._input_port(cell_id),
                next_epoch=self._history.next_epoch,
                commit=self._history.commit,
                publish=lambda snapshot: self._snapshots.publish(cell_id, snapshot),
                extensions=self._extensions,
            )

        return factory

    def _event_sink(self, cell_id: str) -> Callable[[ActorEvent], None]:
        def sink(event: ActorEvent) -> None:
            if event.kind is not ActorEventKind.CHILD_RESTARTED:
                return
            actor = self._find(cell_id)
            if actor is None:
                return
            try:
                actor.tell(CellActorWakeup())
            except Exception:
                return

        return sink

    def _input_port(self, cell_id: str) -> CellActorInputPort:
        if self._input_port_factory is not None:
            return self._input_port_factory(cell_id)
        if self._runtime is None:
            return _RuntimeInputPort(lambda _cell_id: None, _empty_health, cell_id)
        return _RuntimeInputPort(self._runtime.lookup, self._runtime.health, cell_id)

    def _find(self, cell_id: str) -> ActorCell | None:
        try:
            return self._host.find(_CELL_ACTOR_GROUP, cell_id)
        except Exception as error:
            self._log("warn", "cell.actor_find_failed", cell=cell_id,
                      errorType=type(error).__name__)
            return None

    def _stop(self, actor: ActorCell, cell_id: str) -> None:
        try:
            stopped = actor.stop(STOP_TIMEOUT_S)
            if stopped is False:
                self._log("warn", "cell.actor_stop_timeout", cell=cell_id)
        except Exception as error:
            self._log("warn", "cell.actor_stop_failed", cell=cell_id,
                      errorType=type(error).__name__)

    def _retry(self, cell_id: str, reason: str) -> None:
        self._log("info", "cell.actor_retry_requested", cell=cell_id, reason=reason)
        callback = self._request_index_retry
        if callback is None:
            return
        try:
            callback()
        except Exception as error:
            self._log("warn", "cell.actor_retry_failed", cell=cell_id,
                      errorType=type(error).__name__)

    def _diagnose(self, cell_id: str, event: str, **fields: Any) -> None:
        with self._lock:
            if cell_id in self._reported:
                return
            self._reported.add(cell_id)
        self._log("warn", event, cell=cell_id, **fields)

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, "cells", event, **fields)
        except Exception:
            return

    @staticmethod
    def _coerce_ids(value: Iterable[str] | Any) -> set[str]:
        if hasattr(value, "cells"):
            value = value.cells
        if isinstance(value, dict):
            value = value.keys()
        return {str(cell_id) for cell_id in value}


def _empty_health() -> Any:
    return type("Health", (), {"source_stale": False, "detail": None, "updated_at_ms": 0})()


def _wall_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


__all__ = ["CellActorRegistry", "MAILBOX_CAPACITY"]
