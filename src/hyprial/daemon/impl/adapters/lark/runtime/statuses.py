"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import Any, Mapping

from hyprial.kernel import lark_recovery_coverage
from hyprial.kernel import LarkGatewayConfig

from hyprial.daemon.impl.adapters.lark.runtime.commands import (
    _Aggregate,
    _Observation,
)
from hyprial.daemon.impl.adapters.lark.worker.process import (
    ERROR,
    ONLINE,
    STARTING,
)
def _is_online(authority: "_Authority", name: str) -> bool:
    observation = authority.observations.get(name)
    return bool(
        name in authority.desired
        and observation
        and observation.running
        and observation.readiness == ONLINE
        and observation.health.get("streamHealth") not in {"stale", "checking"}
    )


def _build_statuses(
    authority: "_Authority", generation: int
) -> dict[str, dict[str, object]]:
    """The one status builder.

    Both the live actor and the post-stop settlement path publish through
    this. A hand-written minimal dict in the second path dropped nine schema
    fields and hardcoded processRunning=False, which projected a worker that
    was still held and still running as stopped -- the exact confusion this
    module exists to prevent, reintroduced by the code meant to clear it.
    """

    known = set(authority.gateways) | set(authority.observations)
    statuses: dict[str, dict[str, object]] = {}
    for name in known:
        observation = authority.observations.get(name)
        configured = name in authority.gateways
        running = bool(observation and observation.running)
        readiness = observation.readiness if observation else None
        health = dict(observation.health) if observation else {}
        stream_stale = health.get("streamHealth") == "stale"
        stream_checking = health.get("streamHealth") == "checking"
        online = _is_online(authority, name)
        if name in authority.quarantined:
            status = "quarantined"
        elif not configured:
            status = "detached" if running else "stopped"
        elif stream_stale and name in authority.desired and running:
            status = "stale"
        elif stream_checking and name in authority.desired and running:
            status = "checking"
        elif online:
            status = ONLINE
        elif name in authority.desired and running and readiness == STARTING:
            status = STARTING
        elif name in authority.errors:
            status = ERROR
        else:
            status = "stopped"
        statuses[name] = {
            "id": f"lark:{name}",
            "provider": "lark",
            "name": name,
            "status": status,
            "online": online,
            "configured": configured,
            "desired": name in authority.desired,
            "processRunning": running,
            # Passed in, never read off the authority: a handler holds the
            # generation it was built with, and a stale handler publishing the
            # authority's live value would stamp somebody else's generation on
            # its own projection -- which is exactly what generation fencing
            # exists to detect.
            "generation": generation,
            "version": authority.versions.get(name, 0),
            **lark_recovery_coverage(),
            **health,
            **(
                {"pid": observation.pid}
                if running and observation is not None and observation.pid is not None
                else {}
            ),
            **({"error": authority.errors[name]} if name in authority.errors else {}),
            **(
                {"unsettled": authority.unsettled[name]}
                if name in authority.unsettled
                else {}
            ),
        }
        # G3 (v2): while a lifecycle transition is in flight, say who holds
        # the lock and for how long -- "谁锁的、锁了多久" -- so a wedged
        # adapter is visible in `hyprial adapter status` instead of only as
        # "already in progress" refusals.
        held = authority.transitions.get(name)
        if held is not None:
            statuses[name]["lifecycleCorrelationId"] = held
            written = authority.transition_since.get(name)
            if written is not None:
                statuses[name]["lifecycleAgeSeconds"] = max(
                    0, int(time.monotonic() - written)
                )
    return statuses


class _ProjectionStore:
    """Immutable copies written only by the actor and read by IPC threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gateways: tuple[str, ...] = ()
        self._desired: frozenset[str] = frozenset()
        self._statuses: dict[str, dict[str, object]] = {}
        self._errors: dict[str, str] = {}

    def publish(
        self,
        *,
        gateways: Mapping[str, LarkGatewayConfig],
        desired: set[str],
        statuses: Mapping[str, Mapping[str, object]],
        errors: Mapping[str, str],
    ) -> None:
        with self._lock:
            self._gateways = tuple(sorted(gateways))
            self._desired = frozenset(desired)
            self._statuses = {name: dict(value) for name, value in statuses.items()}
            self._errors = dict(errors)

    @property
    def gateway_names(self) -> tuple[str, ...]:
        with self._lock:
            return self._gateways

    @property
    def desired(self) -> frozenset[str]:
        with self._lock:
            return self._desired

    @property
    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def status(self, name: str | None = None) -> tuple[dict[str, object], ...]:
        with self._lock:
            names = [name] if name is not None else sorted(self._statuses)
            return tuple(
                dict(self._statuses[item]) for item in names if item in self._statuses
            )


class _Authority:
    """Generation-stable actor state.

    Only the current actor handler mutates these containers.  The factory reads
    them after the guardian has stopped the failed generation, so reload and
    desired-state decisions survive an actor restart without a second owner.
    """

    def __init__(self, gateways: Mapping[str, LarkGatewayConfig]) -> None:
        self._custody_lock = threading.Lock()
        self.gateways = dict(gateways)
        self.desired: set[str] = set()
        self.observations: dict[str, _Observation] = {}
        self.errors: dict[str, str] = {}
        #: name -> failure code while the worker is still held. Survives a
        #: healthy probe, unlike ``errors``; cleared only by a real stop.
        self.unsettled: dict[str, str] = {}
        self.versions: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.failures: dict[str, deque[float]] = {}
        self.failed_attempts: set[tuple[str, str]] = set()
        self.quarantined: set[str] = set()
        self.transitions: dict[str, str] = {}
        #: name -> monotonic timestamp of the currently held lifecycle
        #: transition (G2: the deadline object is this write-to-pop age).
        self.transition_since: dict[str, float] = {}
        self.health_events: list[dict[str, object]] = []
        self.pending: dict[str, Future[Any]] = {}
        self.aggregates: dict[str, _Aggregate] = {}
        self.stopping = False
        self.generation = 0

    def begin_generation(self) -> int:
        failure = RuntimeError("Lark adapter actor generation restarted")
        with self._custody_lock:
            pending = tuple(self.pending.values())
            aggregates = tuple(self.aggregates.values())
            self.pending.clear()
            self.aggregates.clear()
        for future in pending:
            if not future.done():
                future.set_exception(failure)
        for aggregate in aggregates:
            if not aggregate.future.done():
                aggregate.future.set_exception(failure)
        self.generation += 1
        return self.generation

    def fail_correlation(self, correlation_id: str, error: BaseException) -> None:
        """Last-resort custody cleanup after bounded completion handoff fails."""

        with self._custody_lock:
            future = self.pending.pop(correlation_id, None)
            aggregate = self.aggregates.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_exception(error)
        if aggregate is not None and not aggregate.future.done():
            aggregate.future.set_exception(error)

    def fail_all_custody(self, error: BaseException) -> None:
        """Terminate every admitted caller when the runtime loses completion custody."""

        with self._custody_lock:
            correlations = tuple(set(self.pending) | set(self.aggregates))
        for correlation_id in correlations:
            self.fail_correlation(correlation_id, error)

    def put_pending(self, correlation_id: str, future: Future[Any]) -> None:
        with self._custody_lock:
            self.pending[correlation_id] = future

    def pop_pending(self, correlation_id: str) -> Future[Any] | None:
        with self._custody_lock:
            return self.pending.pop(correlation_id, None)

    def put_aggregate(self, correlation_id: str, aggregate: _Aggregate) -> None:
        with self._custody_lock:
            self.aggregates[correlation_id] = aggregate

    def get_aggregate(self, correlation_id: str) -> _Aggregate | None:
        with self._custody_lock:
            return self.aggregates.get(correlation_id)

    def pop_aggregate(self, correlation_id: str) -> _Aggregate | None:
        with self._custody_lock:
            return self.aggregates.pop(correlation_id, None)
