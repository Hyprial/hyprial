"""Usage refresh admission and projections owned by an actor."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from types import MappingProxyType

from hyprial.kernel import ActorRuntime
from hyprial.kernel import ActorSpec, AdmissionResult
from hyprial.kernel import EffectLane, EffectRequest, EffectCompleted
from hyprial.kernel import GenerationScheduler
from hyprial.biz.impl.usage import UsageCache, SourceSnapshot, SOURCES, REFRESH_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class RefreshUsage:
    generation: int


@dataclass(frozen=True, slots=True)
class FrozenSource:
    snapshot: SourceSnapshot
    extra_json: bytes


class UsageAuthority:
    """One refresh lane owns collectors/backoff; one actor publishes readings."""

    def __init__(self, **options):
        self._collector = UsageCache(**options)
        self._interval = options.get(
            "refresh_interval_seconds", REFRESH_INTERVAL_SECONDS
        )
        self._clock = options.get("clock", time.time)
        self._guard = threading.Lock()
        self._projection: tuple[FrozenSource, ...] = ()
        self._observer = None
        self._closed = False
        self._started = False
        self._active = False
        self._generation = 1
        self._sequence = 0
        self._current_operation = None
        self._runtime = ActorRuntime()
        self._scheduler = GenerationScheduler()
        self._handle = self._runtime.start(
            ActorSpec(
                name="usage-cache",
                handler_factory=lambda: self._receive,
                mailbox_capacity=8,
            )
        )
        self._effects = EffectLane(
            name="usage-collectors",
            execute=self._collect,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    def _collect(self, request):
        self._collector.refresh_once()
        return tuple(
            FrozenSource(
                replace(item, extra=MappingProxyType({})),
                json.dumps(item.extra).encode(),
            )
            for item in self._collector.snapshots()
        )

    def _receive(self, command):
        if isinstance(command, RefreshUsage):
            with self._guard:
                if (
                    self._closed
                    or command.generation != self._generation
                    or self._active
                ):
                    return
                self._active = True
                self._sequence += 1
                self._current_operation = str(self._sequence)
                operation = self._current_operation
            admission = self._effects.submit(
                EffectRequest(operation, command.generation, command)
            )
            if admission is not AdmissionResult.ACCEPTED:
                with self._guard:
                    self._active = False
                self._schedule()
            return
        if not isinstance(command, EffectCompleted):
            raise TypeError("unsupported usage authority command")
        with self._guard:
            current = (
                command.generation == self._generation
                and command.operation_id == self._current_operation
            )
            if current:
                self._active = False
                self._current_operation = None
                if command.error is None and not self._closed:
                    self._projection = command.result
                observer = self._observer if not self._closed else None
            else:
                observer = None
        self._effects.acknowledge(command.operation_id, command.generation)
        if observer is not None:
            try:
                observer()  # production observer admits a watchdog command only
            except Exception:
                pass
        if current:
            self._schedule()

    def _schedule(self):
        with self._guard:
            if self._closed or not self._started:
                return
            self._scheduler.schedule(
                "usage",
                self._generation,
                self._interval,
                lambda generation: self.refresh_once(),
            )

    def set_refresh_observer(self, callback):
        with self._guard:
            self._observer = callback

    def start(self):
        with self._guard:
            if self._started or self._closed:
                return
            self._started = True
        self.refresh_once()

    def refresh_once(self):
        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
            return self._runtime.tell(self._handle, RefreshUsage(self._generation))

    def snapshots(self):
        with self._guard:
            projection = self._projection
        return tuple(
            replace(item.snapshot, extra=json.loads(item.extra_json))
            for item in projection
        )

    def snapshot_payload(self, now_ms=None):
        now = int(self._clock() * 1000) if now_ms is None else now_ms
        snapshots = {item.source: item for item in self.snapshots()}
        return {
            "sources": [
                (
                    snapshots[source]
                    if source in snapshots
                    else SourceSnapshot(source, False, now, reason="not yet fetched")
                ).to_json(now)
                for source in SOURCES
            ]
        }

    def stop(self, *, timeout=2.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        self._scheduler.shutdown(max(0.0, deadline - time.monotonic()))
        # A queued refresh sees closed; accepted collector effects still settle.
        while True:
            state = self._runtime.snapshot(self._handle)
            with self._guard:
                active = self._active
            if not active and state.queued == state.in_flight == 0:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
