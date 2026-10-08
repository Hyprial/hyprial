"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from concurrent.futures import Future
from typing import Callable

from hyprial.kernel import SupervisionPolicy

from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import (
    ADAPTER_START_TIMEOUT,
    START_DEADLINE_SECONDS,
)
from hyprial.daemon.impl.adapters.lark.runtime.commands import (
    AdapterRestoreSummary,
    _Aggregate,
    _DeliverAlarm,
    _DeliverReply,
    _DrainEvents,
    _IoResult,
    _Observation,
    _Reconcile,
    _Refresh,
    _Reload,
    _Remove,
    _Restore,
    _Shutdown,
    _Start,
    _released,
)
from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects
from hyprial.daemon.impl.adapters.lark.runtime.statuses import (
    _Authority,
    _ProjectionStore,
    _build_statuses,
)
from hyprial.daemon.impl.adapters.lark.worker.process import (
    ONLINE,
    STARTING,
)
class _AdapterHandler:
    def __init__(
        self,
        *,
        generation: int,
        authority: _Authority,
        projections: _ProjectionStore,
        effects: _ProcessEffects,
        policy: SupervisionPolicy,
        retry_delay_override: float | None,
        start_deadline: float = START_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.generation = generation
        # The restart back-off is the one decision here a test must be able to
        # drive: it asks "has the delay elapsed", and a test that answers by
        # sleeping is asserting how fast this machine is.  Injected so the
        # window can be crossed deliberately instead of waited for.
        self._clock = clock
        self._authority = authority
        self._gateways = authority.gateways
        self._desired = authority.desired
        self._observations = authority.observations
        self._errors = authority.errors
        self._unsettled = authority.unsettled
        self._versions = authority.versions
        self._retry_after = authority.retry_after
        self._failures = authority.failures
        self._failed_attempts = authority.failed_attempts
        self._quarantined = authority.quarantined
        self._transitions = authority.transitions
        self._transition_since = authority.transition_since
        self._start_deadline = start_deadline
        self._health_events = authority.health_events
        self._projections = projections
        self._effects = effects
        self._policy = policy
        self._retry_delay_override = retry_delay_override
        self._publish()
        retained = (set(self._observations) | set(self._desired)) - set(
            self._transitions
        )
        if retained and generation > 1:
            correlation = f"generation-recovery:{uuid.uuid4().hex}"
            recovery: Future[None] = Future()
            self._authority.put_aggregate(correlation, _Aggregate(
                "refresh",
                recovery,
                set(retained),
            ))
            for name in sorted(retained):
                self._effects.submit(
                    correlation_id=correlation,
                    generation=self.generation,
                    version=self._versions.get(name, 0),
                    name=name,
                    operation="probe",
                )

    def __call__(self, command: object) -> None:
        if isinstance(command, _Start):
            self._start(command)
        elif isinstance(command, _Remove):
            self._remove(command)
        elif isinstance(command, _Reload):
            self._reload(command)
        elif isinstance(command, _Restore):
            self._restore(command)
        elif isinstance(command, _Reconcile):
            self._reconcile(command)
        elif isinstance(command, _Refresh):
            self._refresh(command)
        elif isinstance(command, _DrainEvents):
            events = tuple(self._health_events)
            self._health_events.clear()
            command.result.set_result(events)
        elif isinstance(command, _DeliverReply):
            self._deliver_reply(command)
        elif isinstance(command, _DeliverAlarm):
            self._deliver_alarm(command)
        elif isinstance(command, _Shutdown):
            self._shutdown(command)
        elif isinstance(command, _IoResult):
            self._complete(command)
        else:
            raise TypeError(f"unsupported Lark adapter command: {type(command).__name__}")

    def _next_version(self, name: str) -> int:
        version = self._versions.get(name, 0) + 1
        self._versions[name] = version
        return version

    def _start(self, command: _Start) -> None:
        if self._authority.stopping:
            command.result.set_exception(RuntimeError("adapter runtime is closing"))
            return
        if command.name in self._transitions:
            command.result.set_exception(
                RuntimeError(f"adapter lifecycle is already in progress: {command.name}")
            )
            return
        gateway = self._gateways.get(command.name)
        if gateway is None:
            command.result.set_exception(
                AdapterStartError(f"Lark adapter is not configured: {command.name}")
            )
            return
        observation = self._observations.get(command.name)
        if (
            command.name in self._desired
            and observation is not None
            and observation.running
            and observation.readiness in {STARTING, ONLINE}
        ):
            command.result.set_result(False)
            return
        already_desired = command.name in self._desired
        self._desired.add(command.name)
        self._transitions[command.name] = command.correlation_id
        self._transition_since[command.name] = time.monotonic()
        if command.explicit:
            self._quarantined.discard(command.name)
            self._failures.pop(command.name, None)
            self._retry_after.pop(command.name, None)
        version = self._next_version(command.name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._observations[command.name] = _Observation(
            False,
            STARTING,
            None,
            None,
        )
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            name=command.name,
            operation="spawn" if already_desired else "activate-spawn",
            gateway=gateway,
        )

    def _remove(self, command: _Remove) -> None:
        if command.name in self._transitions:
            command.result.set_exception(
                RuntimeError(f"adapter lifecycle is already in progress: {command.name}")
            )
            return
        existed = command.name in self._desired or command.name in self._observations
        self._transitions[command.name] = command.correlation_id
        self._transition_since[command.name] = time.monotonic()
        self._retry_after.pop(command.name, None)
        self._failures.pop(command.name, None)
        self._quarantined.discard(command.name)
        self._errors.pop(command.name, None)
        version = self._next_version(command.name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=version,
            name=command.name,
            operation="deactivate-stop",
            payload=existed,
        )

    def _reload(self, command: _Reload) -> None:
        if self._transitions:
            command.result.set_exception(
                RuntimeError("adapter lifecycle is in progress during reload")
            )
            return
        fresh = {item.name: item for item in command.channels.gateways}
        previous = self._gateways
        added = sorted(name for name in fresh if name not in previous)
        updated = sorted(
            name for name in fresh if name in previous and fresh[name] != previous[name]
        )
        removed = sorted(name for name in previous if name not in fresh)
        removed_running = sorted(
            name
            for name in removed
            if (observation := self._observations.get(name)) is not None
            and observation.running
        )
        for name in removed:
            self._desired.discard(name)
            self._transitions.pop(name, None)
            self._transition_since.pop(name, None)
            self._retry_after.pop(name, None)
            self._next_version(name)
        self._gateways.clear()
        self._gateways.update(fresh)
        self._publish()
        command.result.set_result(
            {
                "added": added,
                "updated": updated,
                "removed": removed,
                "removedRunning": removed_running,
            }
        )

    def _restore(self, command: _Restore) -> None:
        self._authority.stopping = False
        self._desired.clear()
        self._desired.update(command.names)
        self._transitions.clear()
        self._retry_after.clear()
        correlation = f"restore:{uuid.uuid4().hex}"
        aggregate = _Aggregate(
            "restore",
            command.result,
            set(command.names),
            attempted=len(command.names),
        )
        self._authority.put_aggregate(correlation, aggregate)
        if not command.names:
            self._finish_aggregate(correlation)
            return
        for name in command.names:
            gateway = self._gateways.get(name)
            if gateway is None:
                self._errors[name] = f"Lark adapter is not configured: {name}"
                aggregate.failed += 1
                aggregate.remaining.discard(name)
                continue
            version = self._next_version(name)
            self._observations[name] = _Observation(False, STARTING, None, None)
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=version,
                name=name,
                operation="spawn",
                gateway=gateway,
            )
        self._publish()
        if not aggregate.remaining:
            self._finish_aggregate(correlation)

    def _reconcile(self, command: _Reconcile) -> None:
        self._expire_transition_deadlines()
        if self._authority.stopping or not self._desired:
            command.result.set_result(0)
            return
        correlation = f"reconcile:{uuid.uuid4().hex}"
        names = set(self._desired) - set(self._transitions)
        self._authority.put_aggregate(correlation, _Aggregate(
            "reconcile-probe",
            command.result,
            set(names),
        ))
        if not names:
            self._finish_aggregate(correlation)
            return
        for name in sorted(names):
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=self._versions.get(name, 0),
                name=name,
                operation="probe",
            )

    def _refresh(self, command: _Refresh) -> None:
        names = set(self._gateways) | set(self._observations)
        if not names:
            command.result.set_result(None)
            return
        correlation = f"refresh:{uuid.uuid4().hex}"
        self._authority.put_aggregate(correlation, _Aggregate(
            "refresh",
            command.result,
            set(names),
        ))
        for name in sorted(names):
            self._effects.submit(
                correlation_id=correlation,
                generation=self.generation,
                version=self._versions.get(name, 0),
                name=name,
                operation="probe",
            )

    def _deliver_reply(self, command: _DeliverReply) -> None:
        if not self._effects.delivery_ready(command.name):
            command.result.set_result(False)
            return
        self._authority.put_pending(command.correlation_id, command.result)
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=self._versions.get(command.name, 0),
            name=command.name,
            operation="deliver-reply",
            payload=command.delivery,
        )

    def _deliver_alarm(self, command: _DeliverAlarm) -> None:
        if not self._effects.delivery_ready(command.name):
            command.result.set_result(False)
            return
        self._authority.put_pending(command.correlation_id, command.result)
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=self._versions.get(command.name, 0),
            name=command.name,
            operation="deliver-alarm",
            payload=(
                command.alarm_correlation_id,
                command.text,
                command.idempotency_key,
            ),
        )

    def _shutdown(self, command: _Shutdown) -> None:
        self._authority.stopping = True
        self._desired.clear()
        self._transitions.clear()
        self._transition_since.clear()
        for name in tuple(self._versions):
            self._next_version(name)
        self._authority.put_pending(command.correlation_id, command.result)
        self._publish()
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self.generation,
            version=0,
            name="*",
            operation="shutdown",
            payload=command.deadline,
        )

    def _expire_transition_deadlines(self) -> None:
        """G2: bound how long one lifecycle transition may stay un-popped.

        The deadline object is the transition's write-to-pop age (fd73140a
        v2): a held ``_transitions[name]`` blocks every further lifecycle
        command for that adapter at admission, so an operation whose
        completion never arrives (or arrives only as a stale failure) locks
        the adapter until a daemon restart -- production held it for >20
        minutes on 2026-09-04.  The tick fires one fail-loud event
        (``lark.adapter.lifecycle.timeout``) and releases the lock: the
        pending caller is failed and the name becomes stoppable/startable
        again.  N is measured, not guessed -- see
        :data:`hyprial.daemon.impl.adapters.lark.contracts.lifecycle.START_DEADLINE_SECONDS`.
        """

        now = time.monotonic()
        for name in tuple(self._transitions):
            correlation = self._transitions.get(name)
            if correlation is None:
                continue
            written = self._transition_since.get(name)
            if written is None:
                self._transition_since[name] = now
                continue
            age = now - written
            if age <= self._start_deadline:
                continue
            observation = self._observations.get(name)
            live = self._effects.liveness(name)
            pid: int | None = None
            last_output: str | None = None
            if live is not None:
                if live["pid"] is not None:
                    pid = int(live["pid"])  # type: ignore[arg-type]
                last_output = live["lastSdkOutput"]  # type: ignore[assignment]
            if observation is not None:
                if pid is None and observation.pid is not None:
                    pid = observation.pid
                last_output = observation.last_sdk_output or last_output
            detail = (
                f"Lark adapter {name} lifecycle transition {correlation} did "
                f"not complete within {self._start_deadline:g}s "
                f"(held {age:.0f}s)"
            )
            self._health_events.append(
                {
                    "event": "lark.adapter.lifecycle.timeout",
                    "adapter": name,
                    "correlationId": correlation,
                    "ageSeconds": round(age, 1),
                    "timeoutSeconds": self._start_deadline,
                    **({"pid": pid} if pid is not None else {}),
                    **(
                        {"lastSdkOutput": last_output[-2000:]}
                        if last_output
                        else {}
                    ),
                }
            )
            future = self._authority.pop_pending(correlation)
            if future is not None and not future.done():
                future.set_exception(
                    AdapterStartError(detail, code=ADAPTER_START_TIMEOUT)
                )
            self._transitions.pop(name, None)
            self._transition_since.pop(name, None)
            self._errors[name] = detail
            self._publish()

    def _complete(self, result: _IoResult) -> None:
        # G0 (fd73140a v2): a completion that settles as failure -- stale
        # generation below, stale version after it -- still releases the
        # lifecycle transition its correlation owns.  Both early returns used
        # to fail the caller's custody and return without popping, so the
        # first stale completion locked the adapter forever: production
        # (daemon.jsonl 2026-09-04 09:21Z) held `_transitions` from the
        # user's `lark:start:` write until a daemon restart 22 minutes later.
        if self._transitions.get(result.name) == result.correlation_id:
            self._transitions.pop(result.name, None)
            self._transition_since.pop(result.name, None)
        if result.generation != self.generation:
            self._authority.fail_correlation(
                result.correlation_id,
                RuntimeError("stale Lark completion generation"),
            )
            return
        if result.name != "*" and result.version != self._versions.get(result.name, 0):
            if result.operation == "spawn":
                self._effects.submit(
                    correlation_id=f"discard:{uuid.uuid4().hex}",
                    generation=self.generation,
                    version=self._versions.get(result.name, 0),
                    name=result.name,
                    operation="discard",
                    payload=result.attempt_token,
                )
            self._authority.fail_correlation(
                result.correlation_id,
                RuntimeError("Lark operation was superseded"),
            )
            return
        # The original guarded pop (dev :1405): for normal completions this
        # releases the transition the hoisted pop above already released --
        # kept as its own site so the early returns having their own release
        # is an additive guarantee, not a moved one.
        if self._transitions.get(result.name) == result.correlation_id:
            self._transitions.pop(result.name, None)
            self._transition_since.pop(result.name, None)
        if not _released(result) and result.name != "*":
            # "*" is the shutdown aggregate, not an adapter: recording debt
            # under it created an entry no adapter-scoped settlement could
            # ever clear. The per-adapter debt is written in the shutdown
            # branch below, from the stranded list.
            #
            # Any operation whose release failed, not only activate-spawn:
            # discard and shutdown reach here with the same code.
            # Unsettled custody outlives the probe that happens to succeed
            # next. Reporting this only through _errors let one healthy
            # refresh clear it, so the single structured signal that a live
            # worker is stranded vanished while the worker kept running.
            self._unsettled[result.name] = result.code or "UNSETTLED"
        if result.observation is not None:
            self._observations[result.name] = result.observation
            self._health_events.extend(result.observation.events)
            if result.observation.error:
                self._errors[result.name] = result.observation.error
            elif result.succeeded and result.name not in self._unsettled:
                self._errors.pop(result.name, None)
        aggregate = self._authority.get_aggregate(result.correlation_id)
        if aggregate is not None:
            self._complete_aggregate(result, aggregate)
            self._publish()
            return
        future = self._authority.pop_pending(result.correlation_id)
        if result.operation in {"spawn", "activate-spawn"}:
            if result.succeeded:
                if future is not None and not future.done():
                    future.set_result(True)
            else:
                if result.operation == "activate-spawn" and _released(result):
                    self._desired.discard(result.name)
                    self._observations.pop(result.name, None)
                self._record_failure(
                    result.name,
                    result.detail or "adapter start failed",
                    attempt_token=result.attempt_token,
                )
                if future is not None and not future.done():
                    future.set_exception(
                        AdapterStartError(result.detail or "adapter start failed")
                    )
        elif result.operation in {"stop", "deactivate-stop"}:
            if result.succeeded:
                # The worker is genuinely gone, so the custody debt recorded by
                # an earlier failed rollback is now settled and the record may
                # be dropped.
                self._unsettled.pop(result.name, None)
                if result.operation == "deactivate-stop":
                    self._desired.discard(result.name)
                self._observations.pop(result.name, None)
                if future is not None and not future.done():
                    future.set_result(bool(result.value))
            else:
                self._errors[result.name] = result.detail or "adapter stop failed"
                if future is not None and not future.done():
                    future.set_exception(
                        RuntimeError(result.detail or "adapter stop failed")
                    )
        elif result.operation in {"deliver-reply", "deliver-alarm"}:
            if future is not None and not future.done():
                future.set_result(bool(result.value) if result.succeeded else False)
        elif result.operation == "shutdown":
            stranded = result.value if isinstance(result.value, tuple) else ()
            for adapter in stranded:
                # Per adapter, so the caller can see *which* one is unsettled;
                # the shutdown result carries one name and cannot say that.
                self._unsettled[str(adapter)] = "WORKER_STOP_FAILED"
                self._errors[str(adapter)] = "adapter stop failed during shutdown"
            for adapter in tuple(self._observations):
                if adapter not in stranded:
                    self._observations.pop(adapter, None)
            if future is not None and not future.done():
                future.set_result(None)
        self._publish()

    def _complete_aggregate(self, result: _IoResult, aggregate: _Aggregate) -> None:
        aggregate.remaining.discard(result.name)
        if aggregate.kind == "restore":
            if result.succeeded and result.observation is not None:
                if result.observation.readiness == ONLINE:
                    aggregate.succeeded += 1
            else:
                aggregate.failed += 1
                self._record_failure(
                    result.name,
                    result.detail or "restore failed",
                    attempt_token=result.attempt_token,
                )
        elif aggregate.kind == "refresh":
            pass
        elif aggregate.kind == "reconcile-probe":
            observation = result.observation
            healthy = bool(
                observation
                and observation.running
                and observation.readiness in {ONLINE, STARTING}
            )
            if not healthy:
                self._record_failure(
                    result.name,
                    (observation.error if observation else None)
                    or "adapter process is not running",
                    attempt_token=(observation.attempt_token if observation else None),
                )
        elif aggregate.kind == "reconcile-spawn":
            if result.succeeded:
                aggregate.restarted += 1
            else:
                self._record_failure(
                    result.name,
                    result.detail or "restart failed",
                    attempt_token=result.attempt_token,
                )
        if aggregate.remaining:
            return
        if aggregate.kind == "reconcile-probe":
            self._begin_reconcile_spawns(result.correlation_id, aggregate)
            return
        self._finish_aggregate(result.correlation_id)

    def _begin_reconcile_spawns(
        self, correlation_id: str, aggregate: _Aggregate
    ) -> None:
        now = self._clock()
        restart: set[str] = set()
        for name in sorted(self._desired):
            observation = self._observations.get(name)
            healthy = bool(
                observation
                and observation.running
                and observation.readiness in {ONLINE, STARTING}
            )
            if healthy or name in self._quarantined:
                continue
            # Review round 4 (fd73140a): possession alone holds here.  The
            # tick-start expiry owns ALL age arithmetic -- by the time this
            # spawn phase walks the desired names it has already released
            # every over-age transition, so anything still held below is in
            # flight by construction.  Re-deriving age with this function's
            # own now₂ opened a window the width of the probe phase (and an
            # operator mismatch at age == deadline): the expiry kept the
            # transition while this gate released it -- supersede without
            # the G2 fail-loud promise.  A leaked transition can no longer
            # interlock with this skip: G0 releases it on the stale early
            # return, and G2's deadline releases the never-completing one.
            if name in self._transitions:
                continue
            if now < self._retry_after.get(name, 0.0):
                continue
            if (gateway := self._gateways.get(name)) is None:
                continue
            restart.add(name)
            attempt = len(self._failures.get(name, ())) + 1
            delay = (
                self._retry_delay_override
                if self._retry_delay_override is not None
                else self._policy.delay(attempt, 0.5)
            )
            self._retry_after[name] = now + delay
            version = self._next_version(name)
            self._effects.submit(
                correlation_id=correlation_id,
                generation=self.generation,
                version=version,
                name=name,
                operation="spawn",
                gateway=gateway,
            )
        aggregate.kind = "reconcile-spawn"
        aggregate.remaining = restart
        if not restart:
            self._finish_aggregate(correlation_id)

    def _finish_aggregate(self, correlation_id: str) -> None:
        aggregate = self._authority.pop_aggregate(correlation_id)
        if aggregate is None:
            return
        if aggregate.future.done():
            return
        if aggregate.kind == "restore":
            aggregate.future.set_result(
                AdapterRestoreSummary(
                    aggregate.attempted,
                    aggregate.succeeded,
                    aggregate.failed,
                )
            )
        elif aggregate.kind in {"reconcile-probe", "reconcile-spawn"}:
            aggregate.future.set_result(aggregate.restarted)
        else:
            aggregate.future.set_result(None)

    def _record_failure(
        self,
        name: str,
        detail: str,
        *,
        attempt_token: str | None = None,
    ) -> None:
        if attempt_token is not None:
            key = (name, attempt_token)
            if key in self._failed_attempts:
                self._errors[name] = detail
                return
            self._failed_attempts.add(key)
        now = time.monotonic()
        failures = self._failures.setdefault(name, deque())
        while failures and now - failures[0] > self._policy.restart_window:
            failures.popleft()
        failures.append(now)
        self._errors[name] = detail
        if len(failures) > self._policy.max_restarts:
            self._quarantined.add(name)

    def _is_online(self, name: str) -> bool:
        observation = self._observations.get(name)
        return bool(
            name in self._desired
            and observation
            and observation.running
            and observation.readiness == ONLINE
            and observation.health.get("streamHealth") not in {"stale", "checking"}
        )

    def _publish(self) -> None:
        statuses = _build_statuses(self._authority, self.generation)
        self._projections.publish(
            gateways=self._gateways,
            desired=self._desired,
            statuses=statuses,
            errors=self._errors,
        )
