"""Single-owner actor composition helpers for the daemon boundary."""

from __future__ import annotations

import threading
import time
import uuid
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TypeVar, cast, TYPE_CHECKING

from hyprial.agents import (
    Agent,
    AgentActor,
    AgentBinding,
    AgentRegistry,
    HandoverNotice,
    PinConflictError,
)
from hyprial.agents.ports import (
    AgentEvent,
    AgentDestroySettled,
    AgentMutationCompleted,
    AgentLifecycleReceiptCompleted,
    AgentProjection,
    BindAgentCommand,
    BlockAgentCommand,
    ClearRestoreDispositionCommand,
    CreateAgentCommand,
    CreateHostInvitedAgentCommand,
    CreateTransferHostedAgentCommand,
    DestroyAgentCommand,
    SettleAgentDestroyCommand,
    PinAgentAdapterCommand,
    RecordAgentActivityCommand,
    SetRestoreDispositionCommand,
    ReleaseAgentCommand,
    UnpinAgentAdapterCommand,
    UnblockAgentCommand,
    UpdateAgentCommand,
    RetireAgentLifecycleReceiptCommand,
    ConfirmAgentLifecycleReceiptCommand,
)
from hyprial.contracts import ipc_errors
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.contracts.readiness import ReadinessReport
from hyprial.daemon.api import HarnessResult
from hyprial.actor_runtime import AdmissionResult

from .bounded_cadence import BoundedCadence, CadenceCompleted
from .lifecycle_receipts import (
    ConfirmLifecycleReceiptCommand,
    LifecycleReceiptCompleted,
    RetireLifecycleReceiptCommand,
)

if TYPE_CHECKING:
    from hyprial.daemon.lifecycle_receipts import (
        DomainEffectClaim, LifecycleMutationRequest, LifecycleMutationCompleted,
        LifecycleMutationFailed,
    )
from hyprial.daemon.harness_actor import HarnessRuntimeActor
from hyprial.daemon.harness_ports import (
    BindHarnessLivenessCommand,
    DispatchHarnessDeliveryCommand,
    DrainHarnessFailedCommand,
    DrainHarnessProgressCommand,
    DrainHarnessReadinessCommand,
    DrainHarnessResultsCommand,
    ClaimHarnessResultsCommand,
    SettleHarnessResultCommand,
    ClaimedHarnessResult,
    EnsureHarnessCommand,
    HarnessAdapterRegistrationProjection,
    HarnessDeliveryAdmitted,
    HarnessDesiredStateManaged,
    HarnessFailedDrained,
    HarnessLivenessBound,
    HarnessLaunchProjection,
    HarnessMutationCompleted,
    HarnessProgressObserved,
    HarnessProjectionsRefreshed,
    HarnessReadinessDrained,
    HarnessReadyObserved,
    HarnessRestoreCompleted,
    HarnessResultObserved,
    HarnessResultsClaimed,
    HarnessResultSettled,
    HarnessSessionRefsReconciled,
    HarnessTimerCompleted,
    HarnessTimerElapsedCommand,
    HarnessesStopped,
    RefreshHarnessProjectionsCommand,
    ReconcileHarnessSessionRefsCommand,
    RemoveAdapterRegistrationCommand,
    RemoveHarnessCommand,
    RestoreHarnessesCommand,
    AgentIdentityProjectionPort,
    RestoreEligibilityProjection,
    RestoreAdapterRegistrationCommand,
    UpdateRestoreEligibilityCommand,
    SnapshotAdapterRegistrationCommand,
    StageHarnessDesiredCommand,
    StopHarnessesCommand,
    WaitHarnessReadyCommand,
)
from hyprial.adapters.lark.api import HarnessDelivery
from hyprial.adapters.lark.ports import (
    AdapterIoCompleted,
    AdapterHealthEventsCompleted,
    AdapterTimerElapsedCommand,
    AdapterMutationCompleted,
    DeliverLarkAlarmCommand,
    DeliverLarkMessageCommand,
    DrainAdapterHealthCommand,
    LarkCommand,
    ReloadAdaptersCommand,
    StartAdapterCommand,
    StopAdapterCommand,
)

from .desired_state import DesiredStateError, DesiredStateStore, HarnessLaunchSpec
from .session_actor import SessionActor


# Correlation-key namespace, distinct from canonical Agent URI construction.
_AGENT_RECEIPT_NAMESPACE = "agent:receipt"


class DomainCommandError(RuntimeError):
    """A typed domain command was rejected or did not settle in budget."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _check_domain_receipt(
    event: LifecycleReceiptCompleted,
    domain: str,
    generation: int,
    attempt_token: str,
    resource_token: str,
    operation: str,
) -> None:
    if (
        event.domain != domain
        or event.generation != generation
        or event.attempt_token != attempt_token
        or event.resource_token != resource_token
        or event.operation != operation
    ):
        raise DomainCommandError(
            "STALE_LIFECYCLE_RECEIPT", f"{domain} receipt completion fence mismatch"
        )


class LarkDesiredStatePort:
    """Adapter-effect persistence for daemon-restart recovery intent.

    The Adapter actor is the only caller.  Application handlers never mutate
    Lark desired specs independently from the process effect they requested.
    """

    def __init__(self, store: DesiredStateStore) -> None:
        self._store = store

    def activate(self, name: str) -> bool:
        state = self._store.load()
        if any(
            spec.harness == "lark" and spec.name == name
            for spec in state.harnesses
        ):
            return False
        self._store.upsert_harness(
            HarnessLaunchSpec(harness="lark", name=name, headless=True)
        )
        return True

    def deactivate(self, name: str) -> bool:
        state = self._store.load()
        existed = any(
            spec.harness == "lark" and spec.name == name
            for spec in state.harnesses
        )
        if existed:
            self._store.remove_harness("lark", name)
        return existed


_EventT = TypeVar("_EventT")


class CorrelatedDomainEvents:
    """Bounded correlated event journal for synchronous system edges only."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("event capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._events: OrderedDict[str, list[object]] = OrderedDict()
        self._settlement_reservations: set[str] = set()
        self._closed = False

    def reserve_settlement(self, correlation_id: str) -> None:
        with self._condition:
            if self._closed:
                raise DomainCommandError(
                    "DOMAIN_EVENTS_CLOSED", "domain event owner is closed"
                )
            if correlation_id in self._settlement_reservations:
                raise DomainCommandError(
                    "DOMAIN_CORRELATION_CONFLICT",
                    f"domain correlation is already reserved: {correlation_id}",
                )
            if len(self._settlement_reservations) >= self._capacity:
                raise DomainCommandError(
                    "DOMAIN_EVENT_CAPACITY",
                    "domain settlement reservation capacity is full",
                )
            self._settlement_reservations.add(correlation_id)

    def cancel_settlement(self, correlation_id: str) -> None:
        with self._condition:
            self._settlement_reservations.discard(correlation_id)
            self._events.pop(correlation_id, None)
            self._condition.notify_all()

    def publish(self, event: object) -> None:
        correlation_id = getattr(event, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            raise TypeError("domain event must carry a non-empty correlation_id")
        with self._condition:
            if self._closed:
                return
            self._events.setdefault(correlation_id, []).append(event)
            self._events.move_to_end(correlation_id)
            while len(self._events) > self._capacity:
                evictable = next(
                    (
                        current
                        for current in self._events
                        if current not in self._settlement_reservations
                    ),
                    None,
                )
                if evictable is None:
                    break
                self._events.pop(evictable, None)
            self._condition.notify_all()

    def wait(
        self,
        correlation_id: str,
        expected: type[_EventT],
        *,
        timeout: float,
    ) -> _EventT:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                events = self._events.get(correlation_id, [])
                for event in events:
                    if isinstance(event, PortCommandRejected):
                        self._events.pop(correlation_id, None)
                        raise DomainCommandError(event.code, event.detail)
                    if isinstance(event, expected):
                        self._events.pop(correlation_id, None)
                        return cast(_EventT, event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DomainCommandError(
                        "DOMAIN_COMMAND_TIMEOUT",
                        f"domain command {correlation_id} did not settle",
                    )
                self._condition.wait(remaining)

    def wait_settled(
        self,
        correlation_id: str,
        expected: type[_EventT],
    ) -> _EventT:
        """Join one admitted command without turning a deadline into cancel."""

        with self._condition:
            while True:
                events = self._events.get(correlation_id, [])
                for event in events:
                    if isinstance(event, PortCommandRejected):
                        self._events.pop(correlation_id, None)
                        self._settlement_reservations.discard(correlation_id)
                        raise DomainCommandError(event.code, event.detail)
                    if isinstance(event, expected):
                        self._events.pop(correlation_id, None)
                        self._settlement_reservations.discard(correlation_id)
                        return cast(_EventT, event)
                if self._closed:
                    self._settlement_reservations.discard(correlation_id)
                    raise DomainCommandError(
                        "DOMAIN_EVENTS_CLOSED",
                        f"domain command {correlation_id} lost its event owner",
                    )
                self._condition.wait()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._events.clear()
            self._settlement_reservations.clear()
            self._condition.notify_all()


def _import_legacy_session_agents(
    registry: AgentRegistry, desired_state: DesiredStateStore
) -> tuple[str, ...]:
    """Materialize local Agent records missing from pre-Agent session state.

    Before the Agent entity existed, ``session.register`` persisted only an
    interactive session.  Big-bang SessionActor recovery correctly requires
    every session to name a real local Agent, so a direct upgrade must bridge
    that one historical shape before either actor starts.  Foreign canonical
    URIs remain foreign and fail closed; existing records are never rewritten.
    """

    imported: list[str] = []
    source_harness = {
        "claude-channel": "claude",
        "pi-extension": "pi",
        "codex-app-server": "codex",
    }
    for session in desired_state.load().interactive_sessions:
        actor = registry.local_actor(session.actor)
        if actor is None or registry.exists(actor):
            continue
        runtime = (session.runtime or "").split("_", 1)[0]
        preferred = (
            runtime
            if runtime in {"claude", "pi", "codex", "dsh"}
            else source_harness.get(session.source)
        )
        registry.create(
            actor,
            cwd=session.cwd,
            preferred_harness=preferred,
        )
        imported.append(f"interactiveSession:{session.actor}")
    return tuple(imported)


class AgentSessionDomains:
    """Own the private Agent registry and the Agent/Session actor fan-out."""

    def __init__(
        self,
        *,
        database: Path,
        desired_state: DesiredStateStore,
        owner: str,
        node_id: str,
        daemon_epoch: str,
        hyprial_home: Path,
        worker_running: Callable[..., bool | None],
        clock: Callable[[], float],
        persistence_late_result: Callable[[str], object | None] | None = None,
        session_desired_state: object | None = None,
    ) -> None:
        registry = AgentRegistry(
            database,
            owner=owner,
            machine=node_id,
            hyprial_home=hyprial_home,
        )
        imported_sessions = _import_legacy_session_agents(registry, desired_state)
        self.imported_legacy = (*registry.imported_legacy, *imported_sessions)
        self._registry = registry
        self._domains_closed = False
        self._close_lock = threading.Lock()
        self._agent_events = CorrelatedDomainEvents()
        self._session_events = CorrelatedDomainEvents()
        self._session: SessionActor | None = None

        def agent_event(event: AgentEvent) -> None:
            self._agent_events.publish(event)
            session = self._session
            if session is not None:
                session.accept_agent_event(event)

        def session_originated(correlation_id: str) -> bool:
            session = self._session
            return session is not None and session.owns_agent_correlation(
                correlation_id
            )

        self.agent = AgentActor(
            registry,
            event_sink=agent_event,
            desired_state=desired_state,
            worker_running=worker_running,
            clock=clock,
            session_originated=session_originated,
        )
        self.session = SessionActor(
            desired_state if session_desired_state is None else session_desired_state,
            daemon_epoch=daemon_epoch,
            event_sink=self._session_events.publish,
            agent_commands=self.agent,
            clock=clock,
            persistence_late_result=persistence_late_result,
        )
        self._session = self.session
        self.agents = AgentDirectoryFacade(self)
        self.liveness = AgentLivenessProjectionFacade(self)

    def call_agent(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float = 5.0,
    ) -> _EventT:
        admission = self.agent.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"agent command admission is {admission.value}",
            )
        return self._agent_events.wait(
            str(getattr(command, "correlation_id")), expected, timeout=timeout
        )

    def call_agent_settled(
        self,
        command: object,
        expected: type[_EventT],
    ) -> _EventT:
        """Submit once and join that exact Agent completion until settlement."""

        correlation_id = str(getattr(command, "correlation_id"))
        self._agent_events.reserve_settlement(correlation_id)
        try:
            admission = self.agent.submit(command)  # type: ignore[arg-type]
        except BaseException:
            self._agent_events.cancel_settlement(correlation_id)
            raise
        if admission is not PortAdmission.ACCEPTED:
            self._agent_events.cancel_settlement(correlation_id)
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"agent command admission is {admission.value}",
            )
        return self._agent_events.wait_settled(
            correlation_id, expected
        )

    def call_session(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float = 5.0,
    ) -> _EventT:
        admission = self.session.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"session command admission is {admission.value}",
            )
        return self._session_events.wait(
            str(getattr(command, "correlation_id")), expected, timeout=timeout
        )

    def wait_agent_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._agent_events.wait(correlation_id, expected, timeout=5.0)

    def wait_session_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._session_events.wait(correlation_id, expected, timeout=5.0)

    def retire_agent_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.agent.generation
        event = self.call_agent(
            RetireAgentLifecycleReceiptCommand(
                f"{_AGENT_RECEIPT_NAMESPACE}:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            AgentLifecycleReceiptCompleted,
        )
        self._check_agent_receipt(
            event, generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def expire_lifecycle_receipts(self) -> int:
        """U0c startup sweep passthrough (see AgentRegistry)."""

        return self._registry.expire_lifecycle_receipts()

    def lifecycle_effect_claims(self) -> list["DomainEffectClaim"]:
        """U0c backfill claims passthrough (see AgentRegistry)."""

        return self._registry.lifecycle_effect_claims()

    def confirm_agent_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.agent.generation
        event = self.call_agent(
            ConfirmAgentLifecycleReceiptCommand(
                f"{_AGENT_RECEIPT_NAMESPACE}:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            AgentLifecycleReceiptCompleted,
        )
        self._check_agent_receipt(
            event, generation, attempt_token, resource_token, "confirm"
        )

    @staticmethod
    def _check_agent_receipt(
        event: AgentLifecycleReceiptCompleted,
        generation: int,
        attempt_token: str,
        resource_token: str,
        operation: str,
    ) -> None:
        if (
            event.generation != generation
            or event.attempt_token != attempt_token
            or event.resource_token != resource_token
            or event.operation != operation
        ):
            raise DomainCommandError(
                "STALE_AGENT_RECEIPT", "agent receipt completion fence mismatch"
            )

    def retire_session_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.session.generation
        event = self.call_session(
            RetireLifecycleReceiptCommand(
                f"session:receipt:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
        )
        _check_domain_receipt(
            event, "session", generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def confirm_session_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.session.generation
        event = self.call_session(
            ConfirmLifecycleReceiptCommand(
                f"session:receipt:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
        )
        _check_domain_receipt(
            event, "session", generation, attempt_token, resource_token, "confirm"
        )

    def close(self, timeout: float = 5.0) -> bool:
        with self._close_lock:
            if self._domains_closed:
                return True
            started = time.monotonic()
            session = self.session.drain(timeout * 0.6)
            if not session.complete:
                return False
            remaining = max(0.0, timeout - (time.monotonic() - started))
            agent = self.agent.drain(remaining)
            if not agent.complete:
                return False
            self._session_events.close()
            self._agent_events.close()
            self._registry.close()
            self._domains_closed = True
            return True


class LarkPortClient:
    """System-edge adapter over Lark frozen commands and projections."""

    def __init__(
        self,
        commands: object,
        events: CorrelatedDomainEvents,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._commands = commands
        self._events = events
        self._timeout = timeout

    def call(self, command: LarkCommand, expected: type[_EventT]) -> _EventT:
        admission = self._commands.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"lark command admission is {admission.value}",
            )
        return self._events.wait(
            command.correlation_id, expected, timeout=self._timeout
        )

    def start(self, name: str) -> AdapterMutationCompleted:
        return self.call(
            StartAdapterCommand(f"lark:start:{uuid.uuid4().hex}", name),
            AdapterMutationCompleted,
        )

    def stop(self, name: str) -> AdapterMutationCompleted:
        return self.call(
            StopAdapterCommand(f"lark:stop:{uuid.uuid4().hex}", name),
            AdapterMutationCompleted,
        )

    def reload(self) -> AdapterMutationCompleted:
        return self.call(
            ReloadAdaptersCommand(f"lark:reload:{uuid.uuid4().hex}"),
            AdapterMutationCompleted,
        )

    def timer(self, observed_at_ms: int) -> AdapterMutationCompleted:
        return self.call(
            AdapterTimerElapsedCommand(
                f"lark:timer:{uuid.uuid4().hex}",
                self._commands.generation,
                self._commands.version,
                observed_at_ms,
            ),
            AdapterMutationCompleted,
        )

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        event = self.call(
            DrainAdapterHealthCommand(f"lark:health:{uuid.uuid4().hex}"),
            AdapterHealthEventsCompleted,
        )
        return tuple(dict(item) for item in event.events)

    def read_adapter(self, name: str) -> object | None:
        return self._commands.read_adapter(name)

    def read_adapters(self) -> tuple[object, ...]:
        return tuple(self._commands.read_adapters())

    def reply_online(self, adapter: str) -> bool:
        projection = self._commands.read_adapter(adapter)
        return bool(projection is not None and projection.online)

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool:
        payload = json.dumps(
            {
                "deliveryId": delivery.delivery_id,
                "messageId": delivery.message_id,
                "replyTo": delivery.reply_to,
                "fromActor": {
                    "actorId": delivery.from_actor.actor_id,
                    "actorKey": delivery.from_actor.actor_key,
                    "displayName": delivery.from_actor.display_name,
                },
                "text": delivery.text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        event = self.call(
            DeliverLarkMessageCommand(
                f"lark:deliver:{delivery.delivery_id}:{uuid.uuid4().hex}",
                adapter,
                delivery.reply_to,
                payload,
            ),
            AdapterIoCompleted,
        )
        return event.succeeded

    def deliver_alarm(
        self,
        adapter: str,
        message_id: str,
        text: str,
        *,
        idempotency_key: str,
    ) -> bool:
        event = self.call(
            DeliverLarkAlarmCommand(
                f"lark:alarm:{message_id}:{uuid.uuid4().hex}",
                adapter,
                message_id,
                text,
                idempotency_key,
            ),
            AdapterIoCompleted,
        )
        return event.succeeded


class HarnessPortClient:
    """System-edge waits over the frozen Harness command/event seam."""

    def __init__(
        self,
        actor: HarnessRuntimeActor,
        events: CorrelatedDomainEvents,
        *,
        timeout: float = 65.0,
    ) -> None:
        self.actor = actor
        self._events = events
        self._timeout = timeout
        self._timer_sequence = 0
        self.drain_complete = True
        self._orphan_collection_lock = threading.Lock()
        self._orphan_collection: BoundedCadence[int] | None = None
        self._orphan_completed_sequence = 0
        self._orphan_reported_sequence = 0
        self._orphan_last_retired = 0
        self._orphan_last_error: str | None = None

    @property
    def generation(self) -> int:
        return self.actor.generation

    @property
    def version(self) -> int:
        return self.actor.version

    def call(
        self,
        command: object,
        expected: type[_EventT],
        *,
        timeout: float | None = None,
    ) -> _EventT:
        admission = self.actor.submit(command)  # type: ignore[arg-type]
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"harness command admission is {admission.value}",
            )
        return self._events.wait(
            str(getattr(command, "correlation_id")),
            expected,
            timeout=self._timeout if timeout is None else timeout,
        )

    def wait_lifecycle(
        self, correlation_id: str, expected: type[_EventT]
    ) -> _EventT:
        return self._events.wait(correlation_id, expected, timeout=self._timeout)

    def retire_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        generation = self.actor.generation
        event = self.call(
            RetireLifecycleReceiptCommand(
                f"harness:receipt:retire:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
            timeout=5.0,
        )
        _check_domain_receipt(
            event, "harness", generation, attempt_token, resource_token, "retire"
        )
        return event.matched

    def confirm_lifecycle_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> None:
        generation = self.actor.generation
        event = self.call(
            ConfirmLifecycleReceiptCommand(
                f"harness:receipt:confirm:{uuid.uuid4().hex}",
                attempt_token, resource_token,
            ),
            LifecycleReceiptCompleted,
            timeout=5.0,
        )
        _check_domain_receipt(
            event, "harness", generation, attempt_token, resource_token, "confirm"
        )

    def submit_lifecycle(self, command: object) -> PortAdmission:
        return self.actor.submit(command)

    def fail_lifecycle(
        self, request: LifecycleMutationRequest, code: str, detail: str, timeout: float,
    ) -> LifecycleMutationCompleted | LifecycleMutationFailed:
        from .lifecycle_receipts import (
            FailHarnessLifecycleCommand, HarnessLifecycleFailureSettled,
        )

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            # Each admission has its own edge correlation: an OVERLOADED
            # rejection must not poison the subsequent accepted reply.
            command = FailHarnessLifecycleCommand(
                f"harness:fail:{uuid.uuid4().hex}", request, code, detail,
            )
            admission = self.actor.submit(command)
            remaining = deadline - time.monotonic()
            if admission is PortAdmission.ACCEPTED:
                try:
                    return self._events.wait(
                        command.correlation_id, HarnessLifecycleFailureSettled,
                        timeout=max(0.0, remaining),
                    ).result
                except DomainCommandError as error:
                    raise TimeoutError(f"harness failure settlement unresolved: {error}") from error
            if admission is not PortAdmission.OVERLOADED or remaining <= 0:
                raise TimeoutError(
                    f"harness failure settlement admission is {admission.value}"
                )
            time.sleep(min(0.002, remaining))

    def restore(self, desired: tuple[HarnessLaunchSpec, ...]) -> object:
        return self.call(
            RestoreHarnessesCommand(
                f"harness:restore:{uuid.uuid4().hex}",
                tuple(harness_launch_projection(item) for item in desired),
            ),
            HarnessRestoreCompleted,
        ).result

    def start(self, spec: HarnessLaunchSpec) -> bool:
        return self.call(
            EnsureHarnessCommand(
                f"harness:start:{uuid.uuid4().hex}", harness_launch_projection(spec)
            ),
            HarnessMutationCompleted,
        ).changed

    def remove(self, harness: str, name: str) -> bool:
        return self.call(
            RemoveHarnessCommand(
                f"harness:remove:{uuid.uuid4().hex}", harness, name
            ),
            HarnessMutationCompleted,
        ).changed

    def reconcile(self) -> int:
        self._timer_sequence += 1
        return self.call(
            HarnessTimerElapsedCommand(
                f"harness:timer:{uuid.uuid4().hex}",
                self.generation,
                self._timer_sequence,
                time.time_ns() // 1_000_000,
            ),
            HarnessTimerCompleted,
        ).restarted

    def submit_restore_eligibility(
        self, eligibility: RestoreEligibilityProjection
    ) -> PortAdmission:
        return self.actor.submit(
            UpdateRestoreEligibilityCommand(
                f"harness:restore-eligibility:{uuid.uuid4().hex}", eligibility
            )
        )

    def drain_failed_events(self) -> tuple[str, ...]:
        return self.call(
            DrainHarnessFailedCommand(
                f"harness:failed:{uuid.uuid4().hex}"
            ),
            HarnessFailedDrained,
        ).harness_ids

    def drain_readiness_reports(self) -> tuple[ReadinessReport, ...]:
        return self.call(
            DrainHarnessReadinessCommand(
                f"harness:readiness:{uuid.uuid4().hex}"
            ),
            HarnessReadinessDrained,
        ).reports

    def status(self) -> tuple[dict[str, object], ...]:
        return tuple(item.to_payload() for item in self.actor.read_harnesses())

    def collect_orphans(self) -> int:
        """Admit a bounded probe and report only newly completed retirements."""

        with self._orphan_collection_lock:
            lane = self._orphan_collection
            if lane is None:
                lane = BoundedCadence(
                    "harness-orphan-collection",
                    lambda _at_ms: self.actor.collect_orphans(),
                    self._orphan_collection_completed,
                )
                self._orphan_collection = lane
            if self._orphan_completed_sequence != self._orphan_reported_sequence:
                retired = self._orphan_last_retired
                self._orphan_reported_sequence = self._orphan_completed_sequence
            else:
                retired = 0
        admitted = lane.submit(time.time_ns() // 1_000_000)
        if admitted is AdmissionResult.CLOSED:
            raise DomainCommandError("ORPHAN_COLLECTION_CLOSED", "orphan collection closed")
        return retired

    def _orphan_collection_completed(self, completion: CadenceCompleted[int]) -> None:
        with self._orphan_collection_lock:
            self._orphan_completed_sequence += 1
            self._orphan_last_retired = completion.result or 0
            self._orphan_last_error = (
                None if completion.error is None else str(completion.error)[:500]
            )

    def orphan_collection_status(self) -> dict[str, object]:
        with self._orphan_collection_lock:
            lane = self._orphan_collection
            error = self._orphan_last_error
        if lane is None:
            return {"accepted": 0, "overloaded": 0, "completed": 0, "failed": 0}
        projection = lane.projection()
        return {
            "accepted": projection.accepted,
            "overloaded": projection.overloaded,
            "coalesced": projection.coalesced,
            "completed": projection.completed,
            "failed": projection.failed,
            **({"lastError": error} if error is not None else {}),
        }

    def orphan_status(self) -> tuple[dict[str, object], ...]:
        return self.actor.orphan_status()

    def streaming_actors(self) -> tuple[str, ...]:
        return self.actor.read_streaming().actors

    def streaming_generations(self) -> dict[str, int]:
        return dict(self.actor.read_streaming().process_generations)

    def session_refs(self) -> dict[tuple[str, str], str]:
        self.call(
            RefreshHarnessProjectionsCommand(
                f"harness:refresh:{uuid.uuid4().hex}"
            ),
            HarnessProjectionsRefreshed,
        )
        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_session_refs().refs
        }

    def projected_session_refs(self) -> dict[tuple[str, str], str]:
        """Immutable projection read; never refreshes or waits on the actor."""

        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_session_refs().refs
        }

    def reconcile_session_refs(self) -> dict[tuple[str, str], str]:
        event = self.call(
            ReconcileHarnessSessionRefsCommand(
                f"harness:session-refs:{uuid.uuid4().hex}"
            ),
            HarnessSessionRefsReconciled,
        )
        if event.error is not None:
            raise DesiredStateError(event.error)
        return {
            (item.harness, item.name): item.session_ref for item in event.refs
        }

    def stage_harness_desired(self, spec: HarnessLaunchSpec) -> bool:
        event = self.call(
            StageHarnessDesiredCommand(
                f"harness:stage:{uuid.uuid4().hex}", harness_launch_projection(spec)
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def snapshot_adapter_registration(
        self, name: str
    ) -> HarnessAdapterRegistrationProjection:
        event = self.call(
            SnapshotAdapterRegistrationCommand(
                f"harness:adapter-snapshot:{uuid.uuid4().hex}", name
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        assert event.adapter is not None
        return event.adapter

    def remove_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = self.call(
            RemoveAdapterRegistrationCommand(
                f"harness:adapter-remove:{uuid.uuid4().hex}",
                snapshot.name,
                snapshot.spec,
                snapshot.legacy_pin,
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def restore_adapter_registration(
        self, snapshot: HarnessAdapterRegistrationProjection
    ) -> bool:
        event = self.call(
            RestoreAdapterRegistrationCommand(
                f"harness:adapter-restore:{uuid.uuid4().hex}",
                snapshot.name,
                snapshot.spec,
                snapshot.legacy_pin,
            ),
            HarnessDesiredStateManaged,
        )
        if event.error is not None:
            raise event.error
        return event.changed

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str]:
        """Worker MCP session fences, distinct from native model sessions."""

        return {
            (item.harness, item.name): item.session_ref
            for item in self.actor.read_worker_session_refs().refs
        }

    def wait_ready(self, harness: str, name: str, timeout: float) -> bool:
        return self.call(
            WaitHarnessReadyCommand(
                f"harness:ready:{uuid.uuid4().hex}", harness, name, timeout
            ),
            HarnessReadyObserved,
            timeout=max(timeout + 1.0, self._timeout),
        ).ready

    def dispatch(self, name: str, delivery: object) -> bool:
        return self.call(
            DispatchHarnessDeliveryCommand(
                f"harness:dispatch:{uuid.uuid4().hex}", name, delivery
            ),
            HarnessDeliveryAdmitted,
        ).accepted

    def drain_results(self) -> tuple[HarnessResult, ...]:
        return self.call(
            DrainHarnessResultsCommand(f"harness:results:{uuid.uuid4().hex}"),
            HarnessResultObserved,
        ).results

    def claim_results(self, limit: int = 64) -> tuple[ClaimedHarnessResult, ...]:
        return self.claim_result_batch(limit).claims

    def claim_result_batch(self, limit: int = 64) -> HarnessResultsClaimed:
        return self.call(
            ClaimHarnessResultsCommand(
                f"harness:claim-results:{uuid.uuid4().hex}", limit
            ),
            HarnessResultsClaimed,
        )

    def settle_result(self, claim_token: str, delivery_id: str) -> bool:
        return self.call(
            SettleHarnessResultCommand(
                f"harness:settle-result:{uuid.uuid4().hex}",
                claim_token, delivery_id,
            ),
            HarnessResultSettled,
        ).settled

    def pending_result_delivery_ids(self) -> frozenset[str]:
        return self.actor.read_pending_result_delivery_ids()

    def drain_progress(self) -> tuple[object, ...]:
        return self.call(
            DrainHarnessProgressCommand(f"harness:progress:{uuid.uuid4().hex}"),
            HarnessProgressObserved,
        ).progress

    def bind_liveness(self, harness: str, name: str, binding: object) -> bool:
        return self.call(
            BindHarnessLivenessCommand(
                f"harness:liveness:{uuid.uuid4().hex}", harness, name, binding
            ),
            HarnessLivenessBound,
        ).changed

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        lane = self._orphan_collection
        if lane is not None and not lane.close(max(0.0, deadline - time.monotonic())):
            self.drain_complete = False
            raise RuntimeError("orphan collection did not drain before harness teardown")
        try:
            command_complete = self.call(
                StopHarnessesCommand(
                    f"harness:stop:{uuid.uuid4().hex}", int(deadline * 1000)
                ),
                HarnessesStopped,
                timeout=timeout,
            ).drain_complete
        except BaseException:
            self.drain_complete = False
            raise
        if not command_complete:
            self.drain_complete = False
            return
        runtime_complete = self.actor.close_runtime(
            max(0.0, deadline - time.monotonic())
        )
        self.drain_complete = runtime_complete


def harness_launch_projection(spec: HarnessLaunchSpec) -> HarnessLaunchProjection:
    return HarnessLaunchProjection(
        harness=spec.harness,
        name=spec.name,
        headless=spec.headless,
        args=spec.args,
        ownership=spec.ownership,
        nickname=spec.nickname,
        cwd=spec.cwd,
        endpoint=spec.endpoint,
        session_ref=spec.session_ref,
        command=spec.command,
        turn_timeout_seconds=spec.turn_timeout_seconds,
        idle_timeout_seconds=spec.idle_timeout_seconds,
        containerized=spec.containerized,
        pinned_owner=spec.pinned_owner,
        container_image=spec.container_image,
        model_provider=spec.model_provider,
        model=spec.model,
    )


class AgentDirectoryFacade:
    """Compatibility-shaped system edge; every mutation is an Agent command."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._domains = domains
        self.owner = domains._registry.owner
        self.machine = domains._registry.machine

    @property
    def imported_legacy(self) -> tuple[str, ...]:
        return self._domains.imported_legacy

    @staticmethod
    def normalize_actor(value: str) -> str:
        return AgentRegistry.normalize_actor(value)

    def native_actor(self, value: str) -> str:
        return self._domains._registry.native_actor(value)

    def local_actor(self, value: str) -> str | None:
        from hyprial.uri import parse_agent_uri

        parsed = parse_agent_uri(value)
        if parsed is not None:
            if parsed[0] != self.owner or parsed[1] != self.machine:
                hosted = self._domains.agent.read_agent(parsed[2])
                return (
                    hosted.actor if hosted is not None
                    and hosted.uri == value and hosted.hosted_by is not None
                    else None
                )
            candidate = parsed[2]
        elif ":" in value:
            return None
        else:
            candidate = value
        try:
            return self.normalize_actor(candidate)
        except Exception:
            return None

    def uri_for(self, actor: str) -> str:
        from hyprial.uri import canonical_agent_uri

        name = self.normalize_actor(actor)
        hosted = self._domains.agent.read_agent(name)
        if hosted is not None and hosted.hosted_by is not None:
            return hosted.uri
        return canonical_agent_uri(self.owner, self.machine, name)

    def get(self, actor: str) -> Agent | None:
        local = self.local_actor(actor)
        if local is None:
            return None
        projection = self._domains.agent.read_agent(local)
        return None if projection is None else _agent(projection)

    def projection(self, actor: str) -> AgentProjection | None:
        local = self.local_actor(actor)
        return None if local is None else self._domains.agent.read_agent(local)

    def is_blocked(self, actor: str) -> bool:
        projection = self.projection(actor)
        return bool(projection is not None and projection.block is not None)

    def require(self, actor: str) -> Agent:
        item = self.get(actor)
        if item is None:
            raise DomainCommandError(
                ipc_errors.AGENT_NOT_FOUND,
                f"no agent named {actor!r} on this machine",
            )
        return item

    def exists(self, actor: str) -> bool:
        return self.get(actor) is not None

    def list(self) -> tuple[Agent, ...]:
        return tuple(_agent(item) for item in self._domains.agent.read_agents())

    def projections(self) -> tuple[AgentProjection, ...]:
        return self._domains.agent.read_agents()

    def local_actors(self) -> tuple[str, ...]:
        return tuple(item.uri for item in self._domains.agent.read_agents())

    def pins(self) -> dict[str, str]:
        return {
            adapter: item.uri
            for item in self._domains.agent.read_agents()
            for adapter in item.pinned_adapters
        }

    def create(
        self,
        actor: str,
        *,
        cwd: str | None = None,
        config: object = None,
        provider: str | None = None,
        model: str | None = None,
        capabilities: object = None,
        harness_args: object = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        command = CreateAgentCommand(
                correlation_id=f"agent-command:create:{uuid.uuid4().hex}",
            name=actor,
            cwd=cwd,
            config=config,
            provider=provider,
            model=model,
            capabilities=_capability_pairs(capabilities),
            harness_args=_harness_pairs(harness_args),
            preferred_harness=preferred_harness,
        )
        event = self._domains.call_agent(command, AgentMutationCompleted)
        assert event.agent is not None
        return _agent(event.agent)

    def create_transfer_hosted(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: object = None, preferred_harness: str | None = None,
    ) -> Agent:
        event = self._domains.call_agent(
            CreateTransferHostedAgentCommand(
                correlation_id=f"agent-command:transfer-hosted:{uuid.uuid4().hex}",
                name=actor, pinned_owner=pinned_owner, cwd=cwd,
                harness_args=_harness_pairs(harness_args),
                preferred_harness=preferred_harness,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent)

    def create_host_invited(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: object = None, preferred_harness: str | None = None,
    ) -> Agent:
        from hyprial.contracts import ipc_errors

        correlation_id = f"agent-command:host-invite:{uuid.uuid4().hex}"
        entity_token = uuid.uuid4().hex
        try:
            event = self._domains.call_agent_settled(
                CreateHostInvitedAgentCommand(
                    correlation_id=correlation_id,
                    name=actor, pinned_owner=pinned_owner,
                    entity_token=entity_token, cwd=cwd,
                    harness_args=_harness_pairs(harness_args),
                    preferred_harness=preferred_harness,
                ),
                AgentMutationCompleted,
            )
        except DomainCommandError as error:
            if error.code not in {
                ipc_errors.AGENT_CREATE_COMMIT_FAILED,
                ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
            }:
                raise
            try:
                settlement = self.settle_destroy(
                    f"{correlation_id}:rollback", actor,
                    expected_entity_token=entity_token,
                )
                if settlement.disposition not in {"destroyed", "already-cleaned"}:
                    raise DomainCommandError(
                        ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
                        f"Agent invite {correlation_id} rollback is "
                        f"{settlement.disposition}",
                    )
            except BaseException as cleanup_error:
                raise DomainCommandError(
                    ipc_errors.AGENT_CREATE_ROLLBACK_PENDING,
                    f"Agent invite {correlation_id} failed ({error}); "
                    f"entity {entity_token} cleanup remains unsettled: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                ) from cleanup_error
            if error.code == ipc_errors.AGENT_CREATE_ROLLBACK_PENDING:
                raise DomainCommandError(
                    ipc_errors.AGENT_CREATE_COMMIT_FAILED,
                    f"Agent invite {correlation_id} creation failed and its "
                    f"entity {entity_token} cleanup settled: {error.detail}",
                ) from error
            raise
        assert event.agent is not None
        if event.agent.entity_token != entity_token:
            raise DomainCommandError(
                "DOMAIN_CORRELATION_CONFLICT",
                "host invite completed for a different Agent incarnation",
            )
        return _agent(event.agent)

    def save(self, agent: Agent) -> Agent:
        command = UpdateAgentCommand(
                correlation_id=f"agent-command:update:{uuid.uuid4().hex}",
            name=agent.actor,
            cwd=agent.cwd,
            config=None if agent.config is None else agent.config.to_json(),
            provider=agent.provider,
            model=agent.model,
            capabilities=tuple(sorted(agent.capabilities.items())),
            harness_args=tuple(
                (name, tuple(args)) for name, args in sorted(agent.harness_args.items())
            ),
            preferred_harness=agent.preferred_harness,
            last_harness=agent.last_harness,
            last_session_id=agent.last_session_id,
        )
        event = self._domains.call_agent(command, AgentMutationCompleted)
        assert event.agent is not None
        return _agent(event.agent)

    def record_session(
        self, actor: str, *, harness: str, session_id: str | None
    ) -> HandoverNotice | None:
        agent = self.require(actor)
        notice = (
            HandoverNotice(
                actor=agent.actor,
                previous_harness=agent.last_harness,
                previous_session_id=agent.last_session_id,
                next_harness=harness,
            )
            if agent.last_harness is not None and agent.last_harness != harness
            else None
        )
        self.save(
            replace(
                agent,
                last_harness=harness,
                last_session_id=session_id,
                preferred_harness=agent.preferred_harness or harness,
            )
        )
        return notice

    def record_activity(self, actor: str) -> bool:
        event = self._domains.call_agent(
            RecordAgentActivityCommand(
                correlation_id=f"agent-command:activity:{uuid.uuid4().hex}",
                actor=actor,
            ),
            AgentMutationCompleted,
        )
        return event.changed

    def suppress_restore(
        self,
        actor: str,
        *,
        desired_generation: str,
        last_active_at_ms: int | None,
        idle_age_ms: int | None,
        restore_threshold_ms: int,
        restore_override: str = "none",
        activity_unknown: bool = False,
    ) -> Agent:
        event = self._domains.call_agent(
            SetRestoreDispositionCommand(
                f"agent-command:suppress-restore:{uuid.uuid4().hex}",
                actor,
                desired_generation,
                last_active_at_ms,
                idle_age_ms,
                restore_threshold_ms,
                restore_override,
                activity_unknown,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent)

    def clear_restore_disposition(
        self,
        actor: str,
        *,
        expected_entity_token: str | None = None,
        expected_desired_generation: str | None = None,
        expected_disposition_token: str | None = None,
    ) -> bool:
        return self._domains.call_agent(
            ClearRestoreDispositionCommand(
                f"agent-command:clear-restore:{uuid.uuid4().hex}",
                actor,
                expected_entity_token,
                expected_desired_generation,
                expected_disposition_token,
            ),
            AgentMutationCompleted,
        ).changed

    def block_agent(
        self,
        actor: str,
        *,
        reason: str,
        expected_entity_token: str | None = None,
    ) -> tuple[Agent, bool]:
        event = self._domains.call_agent(
            BlockAgentCommand(
                f"agent-command:block:{uuid.uuid4().hex}",
                actor,
                reason,
                expected_entity_token,
            ),
            AgentMutationCompleted,
        )
        assert event.agent is not None
        return _agent(event.agent), event.changed

    def unblock_agent(self, actor: str) -> bool:
        return self._domains.call_agent(
            UnblockAgentCommand(
                f"agent-command:unblock:{uuid.uuid4().hex}", actor
            ),
            AgentMutationCompleted,
        ).changed

    def destroy(
        self, actor: str, *, expected_entity_token: str | None = None
    ) -> bool:
        event = self._domains.call_agent(
            DestroyAgentCommand(
            f"agent-command:destroy:{uuid.uuid4().hex}",
            actor,
            expected_entity_token,
            ),
            AgentMutationCompleted,
        )
        return event.changed

    def settle_destroy(
        self,
        correlation_id: str,
        actor: str,
        *,
        expected_entity_token: str,
    ) -> AgentDestroySettled:
        """Submit once and join exact Agent/home cleanup settlement."""

        event = self._domains.call_agent_settled(
            SettleAgentDestroyCommand(
                correlation_id,
                actor,
                expected_entity_token,
            ),
            AgentDestroySettled,
        )
        if (
            event.actor != actor
            or event.expected_entity_token != expected_entity_token
        ):
            raise DomainCommandError(
                "DOMAIN_CORRELATION_CONFLICT",
                "Agent destroy settlement does not match the requested entity",
            )
        return event

    def pin(self, adapter: str, actor: str) -> str | None:
        pins = self.pins()
        previous = pins.get(adapter)
        try:
            event = self._domains.call_agent(
                PinAgentAdapterCommand(
            f"agent-command:pin:{uuid.uuid4().hex}", actor, adapter
                ),
                AgentMutationCompleted,
            )
        except DomainCommandError as error:
            if error.code != PinConflictError.code:
                raise
            agent = self.require(actor)
            holder = next(
                (name for name, target in pins.items() if target == agent.uri),
                "(unknown)",
            )
            raise PinConflictError(agent.uri, adapter, holder) from error
        return previous if event.changed else previous

    def restore_pin_if_absent(self, adapter: str, actor: str) -> str | None:
        """Restore one snapshot without overwriting a concurrent replacement."""

        previous = self.pins().get(adapter)
        event = self._domains.call_agent(
            PinAgentAdapterCommand(
                f"agent-command:restore-pin:{uuid.uuid4().hex}",
                actor,
                adapter,
                None,
                True,
            ),
            AgentMutationCompleted,
        )
        return previous if event.changed else previous

    def unpin(self, adapter: str) -> str | None:
        previous = self.pins().get(adapter)
        self._domains.call_agent(
            UnpinAgentAdapterCommand(
            f"agent-command:unpin:{uuid.uuid4().hex}", adapter
            ),
            AgentMutationCompleted,
        )
        return previous

    def unpin_if(self, adapter: str, expected_actor: str | None) -> str | None:
        previous = self.pins().get(adapter)
        self._domains.call_agent(
            UnpinAgentAdapterCommand(
                f"agent-command:unpin-if:{uuid.uuid4().hex}",
                adapter,
                expected_actor,
                True,
            ),
            AgentMutationCompleted,
        )
        return previous

    def close(self) -> None:
        self._domains.close()


class AgentIdentityProjectionView(AgentIdentityProjectionPort):
    """Narrow cache-only Agent incarnation port for other domain owners."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._agent = domains.agent

    def entity_token(self, actor: str) -> str | None:
        projection = self._agent.read_agent(actor)
        return None if projection is None else projection.entity_token


class AgentLivenessProjectionFacade:
    """Read projections plus typed bind/release mutations."""

    def __init__(self, domains: AgentSessionDomains) -> None:
        self._domains = domains

    def binding(self, actor: str) -> AgentBinding | None:
        projection = self._domains.agent.read_binding(actor)
        return None if projection is None else _binding(projection)

    def bindings(self) -> tuple[AgentBinding, ...]:
        values = []
        for agent in self._domains.agent.read_agents():
            binding = self._domains.agent.read_binding(agent.uri)
            if binding is not None:
                values.append(_binding(binding))
        return tuple(values)

    def live_binding(self, actor: str) -> AgentBinding | None:
        runtime = self._domains.agent.read_runtime(actor)
        if runtime.online is False or runtime.binding is None:
            return None
        return _binding(runtime.binding)

    def displaced_by(
        self, actor: str, *, harness: str, runtime: str
    ) -> AgentBinding | None:
        binding = self.binding(actor)
        if binding is None or binding.key == (harness, runtime):
            return None
        return binding

    def bind(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        session_id: str | None = None,
    ) -> AgentBinding:
        event = self._domains.call_agent(
            BindAgentCommand(
                f"agent-command:bind:{uuid.uuid4().hex}",
                actor,
                harness,
                runtime,
                session_id,
            ),
            AgentMutationCompleted,
        )
        assert event.binding is not None
        return _binding(event.binding)

    def release(self, actor: str) -> AgentBinding | None:
        previous = self.binding(actor)
        self._domains.call_agent(
            ReleaseAgentCommand(
                f"agent-command:release:{uuid.uuid4().hex}", actor
            ),
            AgentMutationCompleted,
        )
        return previous

    def touch(self, actor: str) -> None:
        binding = self.binding(actor)
        if binding is None:
            return
        self.bind(
            actor,
            harness=binding.harness,
            runtime=binding.runtime,
            session_id=binding.session_id,
        )

    def verdict(self, actor: str) -> bool | None:
        return self._domains.agent.read_runtime(actor).online

    def status(
        self, actor: str, desired_state: object | None = None
    ) -> str | None:
        """Same verdict as ``verdict``; ``desired_state`` is card 259's

        snapshot-scoped desired-state copy threaded down to the worker probe
        so one snapshot loads the store once.
        """

        runtime = self._domains.agent.read_runtime(actor, desired_state)
        return runtime.status if runtime.online is not None else None

    def registered_status(self, actor: str) -> str:
        return self._domains.agent.read_runtime(actor).status

    def snapshot(self, actor: str) -> dict[str, object]:
        return self._domains.agent.read_runtime(actor).to_payload()


def _agent(value: AgentProjection) -> Agent:
    return Agent(
        uri=value.uri,
        actor=value.actor,
        owner=value.owner,
        machine=value.machine,
        entity_token=value.entity_token,
        cwd=value.cwd,
        config=value.config_payload(),
        provider=value.provider,
        model=value.model,
        capabilities=value.capabilities_payload(),
        harness_args=dict(value.harness_args),
        preferred_harness=value.preferred_harness,
        last_harness=value.last_harness,
        last_session_id=value.last_session_id,
        last_active_at_ms=value.last_active_at_ms,
        pinned_adapters=value.pinned_adapters,
        created_at_ms=value.created_at_ms,
        hosted_by=value.hosted_by,
    )


def _binding(value: object) -> AgentBinding:
    return AgentBinding(
        actor=str(getattr(value, "actor")),
        harness=str(getattr(value, "harness")),
        runtime=str(getattr(value, "runtime")),
        session_id=getattr(value, "session_id"),
        bound_at_ms=int(getattr(value, "bound_at_ms")),
    )


def _capability_pairs(value: object) -> tuple[tuple[str, object], ...]:
    from hyprial.agents import normalize_capabilities

    return tuple(sorted(normalize_capabilities(value).items()))


def _harness_pairs(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        sorted((str(key), tuple(str(item) for item in items)) for key, items in value.items())
    )


__all__ = [
    "AgentSessionDomains",
    "AgentDirectoryFacade",
    "AgentIdentityProjectionView",
    "AgentLivenessProjectionFacade",
    "CorrelatedDomainEvents",
    "DomainCommandError",
    "HarnessPortClient",
    "LarkDesiredStatePort",
    "LarkPortClient",
    "harness_launch_projection",
]
