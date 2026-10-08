"""Actor-owned Lark adapter lifecycle with asynchronous process effects.

The official Lark SDK and websocket client deliberately remain in a dedicated
worker process.  This module owns only daemon-side decisions: configured and
desired adapters, process generations, retry budget, quarantine, and stable
status projections.  Every subprocess/control-socket operation runs on the
effect executor and returns a generation/version-fenced completion to the
actor; no actor handler waits for a process, socket, or network call.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import replace
from typing import Any, Callable, Mapping

from hyprial.kernel import (
    PROCESS_LIFECYCLE,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
)
from hyprial.kernel import DEFAULT_POLICIES
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.kernel import ChannelConfiguration

from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ActorTarget,
    HarnessDelivery,
)
from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import (
    START_DEADLINE_SECONDS,
)
from hyprial.daemon.impl.adapters.lark.ports.ports import (
    AdapterIoCompleted,
    AdapterHealthEventsCompleted,
    AdapterMutationCompleted,
    AdapterProjection,
    AdapterReloadProjection,
    AdapterTimerElapsedCommand,
    DeliverLarkMessageCommand,
    DeliverLarkAlarmCommand,
    DrainAdapterHealthCommand,
    LarkCommand,
    LarkEvent,
    LarkEventSink,
    PinAdapterCommand,
    ReloadAdaptersCommand,
    StartAdapterCommand,
    StopAdapterCommand,
    UnpinAdapterCommand,
)
from hyprial.daemon.impl.adapters.lark.runtime.commands import (
    AdapterDesiredStatePort,
    AdapterRestoreSummary,
    _DeliverAlarm,
    _DeliverReply,
    _DrainEvents,
    _IoResult,
    _Launcher,
    _NullDesiredStatePort,
    _Reconcile,
    _Refresh,
    _Reload,
    _Remove,
    _Restore,
    _Shutdown,
    _Start,
    _WorkerProcess,
)
from hyprial.daemon.impl.adapters.lark.runtime.effects import _ProcessEffects
from hyprial.daemon.impl.adapters.lark.runtime.handler import _AdapterHandler
from hyprial.daemon.impl.adapters.lark.runtime.statuses import (
    _Authority,
    _ProjectionStore,
    _build_statuses,
)
class _NullEventSink:
    def publish(self, event: LarkEvent) -> None:
        del event


class _PortEventDispatcher:
    """Bounded nonblocking handoff from actor completion to event ports."""

    def __init__(self, sink: LarkEventSink, capacity: int = 128) -> None:
        self._sink = sink
        self._slots = threading.BoundedSemaphore(capacity)
        self._queue: queue.Queue[LarkEvent | None] = queue.Queue(maxsize=capacity)
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-lark-port-events",
            daemon=True,
        )
        self._thread.start()

    def reserve(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()

    def publish_reserved(self, event: LarkEvent) -> None:
        # A slot was reserved before command admission, so put_nowait cannot
        # overflow unless the invariant in this class is broken.
        self._queue.put_nowait(event)

    def close(self, timeout: float = 1.0) -> bool:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return False
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._sink.publish(event)
            except Exception:
                pass
            finally:
                self._slots.release()


class AdapterRuntime:
    """Pykka-backed public facade; business callers never receive a raw ref."""

    def __init__(
        self,
        launcher: _Launcher,
        channels: ChannelConfiguration,
        *,
        start_confirm_timeout: float = 2.0,
        restore_confirm_timeout: float | None = None,
        retry_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        mailbox_capacity: int = 128,
        actor_runtime: ActorRuntime | None = None,
        effect_capacity: int = 64,
        event_sink: LarkEventSink | None = None,
        configuration_source: Callable[[], ChannelConfiguration] | None = None,
        desired_state: AdapterDesiredStatePort | None = None,
        start_deadline_seconds: float = START_DEADLINE_SECONDS,
    ) -> None:
        # Legacy knobs remain accepted while composition migrates.  Restart
        # budget/window/backoff come from the one central named profile.
        del restore_confirm_timeout
        self._runtime = actor_runtime or ActorRuntime()
        self._owns_runtime = actor_runtime is None
        self._projections = _ProjectionStore()
        self._events = _PortEventDispatcher(event_sink or _NullEventSink())
        self._configuration_source = configuration_source
        self._reload_slot = threading.Lock()
        self._handle: ActorHandle | None = None
        gateways = {item.name: item for item in channels.gateways}
        self._authority = _Authority(gateways)
        self._policy = DEFAULT_POLICIES[PROCESS_LIFECYCLE]

        def completion(result: _IoResult) -> None:
            handle = self._handle
            if handle is None:
                self._authority.fail_correlation(
                    result.correlation_id,
                    RuntimeError(
                        "Lark completion arrived after adapter runtime shutdown"
                    ),
                )
                return
            result = replace(result, generation=self._authority.generation)
            deadline = time.monotonic() + 5.0
            while True:
                admission = self._runtime.tell(handle, result)
                if admission is AdmissionResult.ACCEPTED:
                    return
                if time.monotonic() >= deadline:
                    self._authority.fail_correlation(
                        result.correlation_id,
                        RuntimeError(
                            "Lark completion custody could not enter the actor mailbox"
                        ),
                    )
                    return
                time.sleep(0.01)

        self._effects = _ProcessEffects(
            launcher,
            completion,
            desired_state or _NullDesiredStatePort(),
            start_confirm_timeout=start_confirm_timeout,
            capacity=effect_capacity,
        )

        def handler_factory() -> _AdapterHandler:
            generation = self._authority.begin_generation()
            return _AdapterHandler(
                generation=generation,
                authority=self._authority,
                projections=self._projections,
                effects=self._effects,
                policy=self._policy,
                retry_delay_override=retry_interval,
                start_deadline=start_deadline_seconds,
                clock=clock,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="lark-adapter-runtime",
                handler_factory=handler_factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="process_lifecycle",
            )
        )

    @property
    def gateway_names(self) -> tuple[str, ...]:
        return self._projections.gateway_names

    @property
    def generation(self) -> int:
        return self._authority.generation

    @property
    def version(self) -> int:
        return max(self._authority.versions.values(), default=0)

    @property
    def last_errors(self) -> dict[str, str]:
        return self._projections.errors

    @property
    def _desired(self) -> frozenset[str]:
        """Read-only compatibility projection; it is not mutable authority."""

        return self._projections.desired

    @property
    def _processes(self) -> dict[str, _WorkerProcess]:
        """Read-only compatibility view of the effect-owned process registry."""

        return self._effects.processes()

    def _submit(self, command: object, future: Future[Any], timeout: float) -> Any:
        handle = self._handle
        if handle is None:
            raise RuntimeError("adapter runtime is stopped")
        admission = self._runtime.tell(handle, command)
        if admission is AdmissionResult.OVERLOADED:
            raise RuntimeError("adapter runtime overloaded")
        if admission is AdmissionResult.CLOSED:
            raise RuntimeError("adapter runtime is closing")
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as error:
            correlation_id = getattr(command, "correlation_id", None)
            if isinstance(correlation_id, str):
                self._authority.fail_correlation(
                    correlation_id,
                    RuntimeError("Lark adapter operation timed out"),
                )
            future.cancel()
            raise RuntimeError("adapter runtime operation timed out") from error

    @staticmethod
    def _port_admission(admission: AdmissionResult) -> PortAdmission:
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        if admission is AdmissionResult.OVERLOADED:
            return PortAdmission.OVERLOADED
        return PortAdmission.CLOSING

    def _port_rejection(
        self,
        correlation_id: str,
        code: str,
        detail: str,
        *,
        admission: PortAdmission | None = None,
    ) -> PortCommandRejected:
        return PortCommandRejected(
            correlation_id=correlation_id,
            domain="lark-adapter",
            generation=self._authority.generation,
            version=max(self._authority.versions.values(), default=0),
            code=code,
            detail=detail,
            admission=admission,
        )

    def _admit_port_future(
        self,
        internal: object,
        future: Future[Any],
        correlation_id: str,
        completed: Callable[[Any], LarkEvent],
    ) -> PortAdmission:
        handle = self._handle
        if handle is None:
            return PortAdmission.CLOSING
        if not self._events.reserve():
            return PortAdmission.OVERLOADED
        admission = self._runtime.tell(handle, internal)
        port_admission = self._port_admission(admission)
        if port_admission is not PortAdmission.ACCEPTED:
            self._events.release()
            return port_admission

        def settled(done: Future[Any]) -> None:
            try:
                event = completed(done.result())
            except BaseException as error:
                event = self._port_rejection(
                    correlation_id,
                    # A typed domain verdict (ADAPTER_ALREADY_RUNNING /
                    # ADAPTER_START_TIMEOUT) keeps its stable code; anything
                    # else degrades to the exception type name as before.
                    getattr(error, "code", None) or type(error).__name__,
                    str(error),
                )
            self._events.publish_reserved(event)

        future.add_done_callback(settled)
        return PortAdmission.ACCEPTED

    def submit(self, command: LarkCommand) -> PortAdmission:
        """Nonblocking frozen-port command admission.

        Pin commands intentionally terminate here with a typed rejection: pin
        authority is the Agent domain and integration's composite sink routes
        those commands there.  This adapter actor never mutates pin state.
        """

        if isinstance(command, (PinAdapterCommand, UnpinAdapterCommand)):
            if not self._events.reserve():
                return PortAdmission.OVERLOADED
            self._events.publish_reserved(
                self._port_rejection(
                    command.correlation_id,
                    "PIN_OWNED_BY_AGENT_DOMAIN",
                    "adapter pin mutations must be routed to the Agent actor",
                )
            )
            return PortAdmission.ACCEPTED
        if isinstance(command, StartAdapterCommand):
            result: Future[bool] = Future()
            return self._admit_port_future(
                _Start(command.correlation_id, command.name, result),
                result,
                command.correlation_id,
                lambda changed: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.name)) else 0),
                    changed=bool(changed),
                    adapter=projection,
                ),
            )
        if isinstance(command, StopAdapterCommand):
            result = Future()
            return self._admit_port_future(
                _Remove(command.correlation_id, command.name, result),
                result,
                command.correlation_id,
                lambda changed: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.name)) else 0),
                    changed=bool(changed),
                    adapter=projection,
                ),
            )
        if isinstance(command, ReloadAdaptersCommand):
            return self._submit_reload_port(command)
        if isinstance(command, AdapterTimerElapsedCommand):
            current_version = max(self._authority.versions.values(), default=0)
            if (
                command.generation != self._authority.generation
                or command.version != current_version
            ):
                if not self._events.reserve():
                    return PortAdmission.OVERLOADED
                self._events.publish_reserved(
                    self._port_rejection(
                        command.correlation_id,
                        "STALE_TIMER",
                        "adapter timer generation/version is stale",
                    )
                )
                return PortAdmission.ACCEPTED
            result = Future()
            return self._admit_port_future(
                _Reconcile(result),
                result,
                command.correlation_id,
                lambda restarted: AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    changed=bool(restarted),
                ),
            )
        if isinstance(command, DrainAdapterHealthCommand):
            result = Future()
            return self._admit_port_future(
                _DrainEvents(result),
                result,
                command.correlation_id,
                lambda events: AdapterHealthEventsCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    events=tuple(
                        tuple(sorted(dict(event).items())) for event in events
                    ),
                ),
            )
        if isinstance(command, DeliverLarkMessageCommand):
            try:
                delivery = self._decode_delivery(command.message_json)
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
                if not self._events.reserve():
                    return PortAdmission.OVERLOADED
                self._events.publish_reserved(
                    self._port_rejection(
                        command.correlation_id,
                        "INVALID_LARK_DELIVERY",
                        str(error),
                    )
                )
                return PortAdmission.ACCEPTED
            result = Future()
            return self._admit_port_future(
                _DeliverReply(
                    command.correlation_id,
                    command.adapter,
                    delivery,
                    result,
                ),
                result,
                command.correlation_id,
                lambda succeeded: AdapterIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(projection.version if (projection := self.read_adapter(command.adapter)) else 0),
                    adapter=command.adapter,
                    operation="deliver",
                    succeeded=bool(succeeded),
                    code=None if succeeded else "DELIVERY_REJECTED",
                ),
            )
        if isinstance(command, DeliverLarkAlarmCommand):
            result = Future()
            return self._admit_port_future(
                _DeliverAlarm(
                    command.correlation_id,
                    command.adapter,
                    command.message_id,
                    command.text,
                    command.idempotency_key,
                    result,
                ),
                result,
                command.correlation_id,
                lambda succeeded: AdapterIoCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=(
                        projection.version
                        if (projection := self.read_adapter(command.adapter))
                        else 0
                    ),
                    adapter=command.adapter,
                    operation="alarm",
                    succeeded=bool(succeeded),
                    code=None if succeeded else "DELIVERY_REJECTED",
                ),
            )
        raise TypeError(f"unsupported Lark port command: {type(command).__name__}")

    def _submit_reload_port(self, command: ReloadAdaptersCommand) -> PortAdmission:
        if not self._events.reserve():
            return PortAdmission.OVERLOADED
        source = self._configuration_source
        if source is None:
            self._events.publish_reserved(
                self._port_rejection(
                    command.correlation_id,
                    "RELOAD_REQUIRES_CONFIGURATION_PORT",
                    "composition must provide the Lark configuration I/O port",
                )
            )
            return PortAdmission.ACCEPTED
        if not self._reload_slot.acquire(blocking=False):
            self._events.release()
            return PortAdmission.OVERLOADED

        def reload_effect() -> None:
            try:
                summary = self.reload_gateways(source())
                event: LarkEvent = AdapterMutationCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._authority.generation,
                    version=max(self._authority.versions.values(), default=0),
                    changed=any(summary.values()),
                    reload=AdapterReloadProjection(
                        added=tuple(summary["added"]),
                        updated=tuple(summary["updated"]),
                        removed=tuple(summary["removed"]),
                        removed_running=tuple(summary["removedRunning"]),
                    ),
                )
            except BaseException as error:
                event = self._port_rejection(
                    command.correlation_id,
                    type(error).__name__,
                    str(error),
                )
            finally:
                self._reload_slot.release()
            self._events.publish_reserved(event)

        threading.Thread(
            target=reload_effect,
            name="hyprial-lark-config-reload",
            daemon=True,
        ).start()
        return PortAdmission.ACCEPTED

    @staticmethod
    def _decode_delivery(payload: bytes) -> HarnessDelivery:
        if len(payload) > 1024 * 1024:
            raise ValueError("Lark delivery JSON exceeds 1 MiB")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Lark delivery JSON must be an object")
        actor = value.get("fromActor")
        if not isinstance(actor, dict):
            raise ValueError("Lark delivery fromActor must be an object")

        def text(record: Mapping[str, object], key: str) -> str:
            item = record.get(key)
            if not isinstance(item, str) or not item:
                raise ValueError(f"Lark delivery {key} must be a non-empty string")
            return item

        return HarnessDelivery(
            delivery_id=text(value, "deliveryId"),
            message_id=text(value, "messageId"),
            reply_to=text(value, "replyTo"),
            from_actor=ActorTarget(
                actor_id=text(actor, "actorId"),
                actor_key=text(actor, "actorKey"),
                display_name=text(actor, "displayName"),
            ),
            text=text(value, "text"),
        )

    def read_adapter(self, name: str) -> AdapterProjection | None:
        statuses = self._projections.status(name)
        if not statuses:
            return None
        value = statuses[0]
        return AdapterProjection(
            version=int(value.get("version", 0)),
            adapter_id=str(value["id"]),
            name=str(value["name"]),
            status=str(value["status"]),
            online=bool(value["online"]),
            configured=bool(value["configured"]),
            desired=bool(value["desired"]),
            process_running=bool(value["processRunning"]),
            pid=(int(value["pid"]) if "pid" in value else None),
            error=(str(value["error"]) if "error" in value else None),
            lifecycle_correlation_id=(
                str(value["lifecycleCorrelationId"])
                if "lifecycleCorrelationId" in value
                else None
            ),
            lifecycle_age_seconds=(
                int(value["lifecycleAgeSeconds"])
                if "lifecycleAgeSeconds" in value
                else None
            ),
        )

    def read_adapters(self) -> tuple[AdapterProjection, ...]:
        return tuple(
            projection
            for value in self._projections.status()
            if (projection := self.read_adapter(str(value["name"]))) is not None
        )

    def reload_gateways(self, channels: ChannelConfiguration) -> dict[str, list[str]]:
        result: Future[dict[str, list[str]]] = Future()
        return self._submit(_Reload(channels, result), result, 5.0)

    def restore(self, specs: tuple[Any, ...]) -> AdapterRestoreSummary:
        names = tuple(sorted(spec.name for spec in specs if spec.harness == "lark"))
        result: Future[AdapterRestoreSummary] = Future()
        timeout = max(5.0, len(names) * 0.5 + 5.0)
        return self._submit(_Restore(names, result), result, timeout)

    def start(self, name: str) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(self._submit(_Start(correlation, name, result), result, 10.0))

    def remove(self, name: str) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(self._submit(_Remove(correlation, name, result), result, 10.0))

    def reconcile(self) -> int:
        result: Future[int] = Future()
        return int(self._submit(_Reconcile(result), result, 10.0))

    def _refresh(self) -> None:
        result: Future[None] = Future()
        self._submit(_Refresh(result), result, 5.0)

    def status(self, name: str | None = None) -> tuple[dict[str, object], ...]:
        if self._handle is not None:
            self._refresh()
        return self._projections.status(name)

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        self._refresh()
        result: Future[tuple[dict[str, object], ...]] = Future()
        return self._submit(_DrainEvents(result), result, 5.0)

    def reply_online(self, adapter: str) -> bool:
        # Reply delivery immediately follows this read.  Refreshing the
        # external worker here turns one reply into two serialized actor
        # operations (refresh, then deliver) and can consume the inbox
        # system-edge completion budget while an inbound event is still being
        # processed.  This precheck means only "the bridge is configured and
        # routable"; ``deliver_reply`` performs the authoritative live and
        # generation-fenced check inside the actor.  A just-started runtime
        # may not have published a status row yet, so stale/empty status must
        # never reject a valid configured bridge before that final check.
        return adapter in self._projections.gateway_names

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(
            self._submit(
                _DeliverReply(correlation, adapter, delivery, result),
                result,
                15.0,
            )
        )

    def deliver_alarm(
        self,
        adapter: str,
        correlation_id: str,
        text: str,
        *,
        idempotency_key: str,
    ) -> bool:
        result: Future[bool] = Future()
        correlation = uuid.uuid4().hex
        return bool(
            self._submit(
                _DeliverAlarm(
                    correlation,
                    adapter,
                    correlation_id,
                    text,
                    idempotency_key,
                    result,
                ),
                result,
                15.0,
            )
        )

    def _settle_released_custody(self) -> None:
        """Settle exactly the adapters this retry released -- no others.

        The actor is stopped, so no completion clears debt or republishes.
        The first version cleared every gateway and hardcoded
        processRunning=False, which reported a worker that was still held and
        still running as stopped: a mixed result (one released, one refusing)
        came out as if both had stopped.

        Publishing goes through the shared builder so the schema and the
        status derivation stay identical to the live actor's.
        """

        held = set(self._effects.retained())
        authority = self._authority
        for adapter in tuple(authority.unsettled):
            # "*" is the shutdown aggregate, not an adapter; it can never be
            # settled by name, so it is dropped here as well.
            if adapter == "*" or adapter not in held:
                authority.unsettled.pop(adapter, None)
                authority.errors.pop(adapter, None)
                authority.observations.pop(adapter, None)
        for adapter in tuple(authority.observations):
            if adapter not in held:
                authority.observations.pop(adapter, None)
        self._projections.publish(
            gateways=authority.gateways,
            desired=authority.desired,
            statuses=_build_statuses(authority, authority.generation),
            errors=authority.errors,
        )

    def stop(self, timeout: float = 5.0) -> bool:
        handle = self._handle
        if handle is None:
            deadline = time.monotonic() + max(0.0, timeout)
            # Drain first. An earlier stop may still be running in its own
            # thread, and retrying then would call process.stop() on the same
            # worker concurrently -- a different bug from the one the retry
            # exists to fix. Only custody that is still held after every
            # in-flight stop finished has genuinely failed.
            effects_complete = self._effects.close(
                max(0.0, deadline - time.monotonic())
            )
            stranded = self._effects.stop_retained(
                max(0.0, deadline - time.monotonic())
            )
            self._settle_released_custody()
            events_complete = self._events.close(
                max(0.0, deadline - time.monotonic())
            )
            if stranded:
                raise RuntimeError(
                    "Lark adapter runtime still holds "
                    f"{', '.join(stranded)}: worker stop did not succeed"
                )
            if not effects_complete or not events_complete:
                raise RuntimeError(
                    "Lark adapter runtime still has unresolved effect custody"
                )
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        result: Future[None] = Future()
        correlation = uuid.uuid4().hex
        self._effects.begin_shutdown()
        command_complete = False
        runtime_complete = False
        effects_complete = False
        events_complete = False
        try:
            self._submit(
                _Shutdown(correlation, result, deadline),
                result,
                max(0.0, deadline - time.monotonic()),
            )
            command_complete = True
        finally:
            try:
                effects_complete = self._effects.close(
                    max(0.0, deadline - time.monotonic())
                )
            finally:
                try:
                    runtime_complete = self._runtime.stop(
                        handle,
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                finally:
                    self._authority.fail_all_custody(
                        RuntimeError("Lark adapter runtime stopped before completion")
                    )
                    self._handle = None
                    events_complete = self._events.close(
                        max(0.0, deadline - time.monotonic())
                    )
        complete = (
            command_complete
            and runtime_complete
            and effects_complete
            and events_complete
        )
        # Ordered after the drain check on purpose: a stop still in progress
        # must keep reporting the drain timeout, which is a different
        # condition with its own contract. This one means everything finished
        # and custody is *still* held -- the stop genuinely failed.
        if complete:
            stranded = self._effects.retained()
            if stranded:
                raise RuntimeError(
                    "Lark adapter runtime still holds "
                    f"{', '.join(stranded)}: worker stop did not succeed"
                )
        if not complete:
            raise RuntimeError(
                "Lark adapter runtime did not drain before deadline "
                f"(command={command_complete}, actor={runtime_complete}, "
                f"effects={effects_complete}, events={events_complete})"
            )
        return True


__all__ = [
    "AdapterRestoreSummary",
    "AdapterRuntime",
    "AdapterStartError",
]
