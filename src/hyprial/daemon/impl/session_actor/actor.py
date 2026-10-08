"""SessionActor: the public session projection/command actor and its lease helpers."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from hyprial.kernel import (
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    DrainReport,
)
from hyprial.kernel import EffectCompleted
from hyprial.identity import (
    AgentEvent,
    AgentMutationCompleted,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import (
    CHANNEL_LIVENESS_TTL_SECONDS,
)
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.desired_state  import (
    DesiredStateStore,
    PendingSessionAgentEffect,
)
from hyprial.daemon.impl.desired_state_io  import (
    DesiredStateIoCompleted,
    DesiredStateIoPort,
    DesiredStateIoRequest,
)
from hyprial.kernel import CallCostCounters
from hyprial.daemon.impl.operations.session_ports  import (
    SessionProjection,
    SessionRuntimeProjection,
)
from hyprial.daemon.impl.state_persistence  import StateCommandCompleted, StateCostOrigin

from .generation import (
    _SessionGeneration,
)
from .internals import (
    SessionOwnershipError,
    _AgentEffectResult,
    _AgentEffectUnavailable,
    _CommandSubmitter,
    _EffectCustody,
    _PendingMutation,
    _ReplayScan,
    _SESSION_COST_KEYS,
    _SessionProjectionState,
    _SessionRuntimeState,
    _Version,
    _projection,
    _publish,
    owner_only_relocation,
)
from .worker import (
    _AgentEffectWorker,
)


class SessionActor:
    """Typed command/projection boundary for interactive-session ownership.

    Wiring seam: the Agent actor's event sink must fan out every ``AgentEvent``
    to :meth:`accept_agent_event`; admission alone is never session success.
    """

    def __init__(
        self,
        store: DesiredStateStore,
        *,
        daemon_epoch: str,
        event_sink: object,
        agent_commands: _CommandSubmitter | None = None,
        clock: Callable[[], float] | None = None,
        clock_ms: Callable[[], int] | None = None,
        lease_ttl_seconds: float = CHANNEL_LIVENESS_TTL_SECONDS,
        mailbox_capacity: int = 128,
        effect_capacity: int = 64,
        effect_deadline: float = 1.0,
        effect_backoff: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
        runtime: ActorRuntime | None = None,
        persistence_late_result: (
            Callable[[str], StateCommandCompleted | None] | None
        ) = None,
    ) -> None:
        if not daemon_epoch:
            raise ValueError("daemon_epoch must not be empty")
        if clock is not None and clock_ms is not None:
            raise ValueError("pass clock or clock_ms, not both")
        self._store = store
        self._events = event_sink
        self._clock_ms = (
            clock_ms
            if clock_ms is not None
            else (
                (lambda: int(clock() * 1000))
                if clock is not None
                else (lambda: time.time_ns() // 1_000_000)
            )
        )
        self._lease_ttl_seconds = lease_ttl_seconds
        self._version = _Version()
        self._runtime_projection = _SessionRuntimeState(daemon_epoch)
        self._session_projection = _SessionProjectionState(
            self._store.load().interactive_sessions
        )
        # Owned by the actor, not the generation: a guardian restart mints a
        # new _SessionGeneration and the totals must survive it.
        self._command_costs = CallCostCounters(_SESSION_COST_KEYS, wall=False)
        self._runtime = runtime or ActorRuntime()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._effect_lock = threading.Lock()
        self._effect_custody: dict[str, _EffectCustody] = {}
        self._agent_attempts: dict[str, tuple[str, str]] = {}
        self._replay_lock = threading.Lock()
        self._replayed_generations: set[int] = set()
        self._replay_attempted: dict[int, threading.Event] = {}
        self._undelivered_lock = threading.Lock()
        self._undelivered: deque[object] = deque()
        self._undelivered_replay_running = False
        self._draining = False
        self._persistence: DesiredStateIoPort | None = None
        # Actor-owned custody survives handler-generation replacement.
        self._deferred_persistence: dict[str, DesiredStateIoRequest] = {}
        self._mutations: dict[str, _PendingMutation] = {}
        self._effect_owners: dict[str, str] = {}

        def persist(request: DesiredStateIoRequest) -> AdmissionResult:
            port = self._persistence
            return (
                AdmissionResult.CLOSED
                if port is None
                else port.submit(request)
            )

        def acknowledge_persistence(request: DesiredStateIoRequest) -> bool:
            port = self._persistence
            return False if port is None else port.acknowledge(request)

        def redeliver_persistence(request: DesiredStateIoRequest) -> None:
            port = self._persistence
            if port is not None:
                port.redeliver(request)

        def factory() -> _SessionGeneration:
            with self._generation_lock:
                self._generation += 1
                generation = self._generation
            handler = _SessionGeneration(
                generation=generation,
                store=self._store,
                daemon_epoch=daemon_epoch,
                events=self._events,
                request_effect=self._request_effect,
                retire_effect=self._retire_effect,
                effect_cost_origin=self._effect_cost_origin,
                version=self._version,
                clock_ms=self._clock_ms,
                lease_ttl_seconds=self._lease_ttl_seconds,
                runtime_projection=self._runtime_projection,
                session_projection=self._session_projection,
                persist=persist,
                acknowledge_persistence=acknowledge_persistence,
                redeliver_persistence=redeliver_persistence,
                command_costs=self._command_costs,
                deferred_persistence=self._deferred_persistence,
                deferred_capacity=mailbox_capacity,
                _mutations=self._mutations,
                _effect_owners=self._effect_owners,
            )
            # Generation 1 is reconciled synchronously after the worker and
            # stable handle exist.  Guardian-created generations need their
            # own hook: handler_factory runs before the backend endpoint is
            # RUNNING, so a small fenced waiter performs replay only after the
            # guardian publishes that exact generation as runnable.
            if generation > 1:
                with self._replay_lock:
                    self._replay_attempted[generation] = threading.Event()
                self._schedule_generation_replay(generation)
            return handler

        self._handle = self._runtime.start(
            ActorSpec(
                name="session-authority",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                undelivered_sink=self._on_undelivered,
            )
        )
        self._persistence = DesiredStateIoPort(
            self._store,
            complete=lambda event: self._runtime.tell(self._handle, event),
            late_result=persistence_late_result,
            capacity=mailbox_capacity,
        )
        self._effect_worker = _AgentEffectWorker(
            agent_commands,
            self._deliver_internal,
            self._submission_failed,
            capacity=effect_capacity,
            deadline=effect_deadline,
            backoff=effect_backoff,
        )
        self.reconcile_pending_effects()

    def _on_undelivered(self, command: object, reason_code: str) -> None:
        if isinstance(command, EffectCompleted):
            # DesiredStateIoPort retains and retries this exact completion
            # until the replacement generation acknowledges it.
            if isinstance(command.result, DesiredStateIoCompleted):
                persistence = self._persistence
                if persistence is not None:
                    persistence.redeliver(command.result.request)
            return
        if isinstance(command, (_AgentEffectResult, _AgentEffectUnavailable)):
            with self._undelivered_lock:
                self._undelivered.append(command)
                if self._undelivered_replay_running:
                    return
                self._undelivered_replay_running = True
            threading.Thread(
                target=self._replay_undelivered,
                name="hyprial-session-undelivered",
                daemon=True,
            ).start()
            return
        with self._generation_lock:
            generation = self._generation
        _publish(
            self._events,
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="session",
                generation=generation,
                version=self.version,
                code=reason_code,
                detail="accepted session command did not begin before actor restart",
            ),
        )

    def _replay_undelivered(self) -> None:
        while True:
            with self._undelivered_lock:
                if not self._undelivered:
                    self._undelivered_replay_running = False
                    return
                command = self._undelivered[0]
            if self._draining:
                if isinstance(command, (_AgentEffectResult, _AgentEffectUnavailable)):
                    self._submission_failed(
                        command.effect_id, command.custody_token
                    )
                with self._undelivered_lock:
                    if self._undelivered and self._undelivered[0] is command:
                        self._undelivered.popleft()
                continue
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                with self._undelivered_lock:
                    if self._undelivered and self._undelivered[0] is command:
                        self._undelivered.popleft()
                continue
            time.sleep(0.005)

    @property
    def generation(self) -> int:
        snapshot = self._runtime.snapshot(self._handle)
        if snapshot.generation > 1:
            deadline = time.monotonic() + 1.0
            attempted = None
            while attempted is None and time.monotonic() < deadline:
                with self._replay_lock:
                    attempted = self._replay_attempted.get(snapshot.generation)
                if attempted is None:
                    current = self._runtime.snapshot(self._handle)
                    if (
                        current.generation != snapshot.generation
                        or current.state
                        in {ActorState.QUARANTINED, ActorState.STOPPED}
                    ):
                        return current.generation
                    time.sleep(0.005)
            if attempted is not None:
                # Expose a restarted generation only after its automatic
                # replay hook has audited durable custody at least once.  This
                # keeps RUNNING observable without letting an external reader
                # race ahead and consume the very recovery fault the hook must
                # handle.
                attempted.wait(max(0.0, deadline - time.monotonic()))
        return snapshot.generation

    @property
    def version(self) -> int:
        return self._version.read()

    @property
    def command_costs(self) -> CallCostCounters:
        """Per-command-type thread CPU on this actor (``ps`` ipcStats)."""

        return self._command_costs

    @property
    def effect_admission_costs(self) -> CallCostCounters:
        """Thread CPU of the Agent-effect admission lane (``ps`` ipcStats)."""

        return self._effect_worker.admission_costs

    @property
    def persistence_io_costs(self) -> CallCostCounters:
        """Thread CPU on this session owner's desired-state I/O lane."""

        persistence = self._persistence
        assert persistence is not None
        return persistence.command_costs

    def owns_agent_correlation(self, correlation_id: str) -> bool:
        """Is ``correlation_id`` an Agent command one of this actor's effects sent?

        Authoritative, not a guess from the id's shape: the attempt is
        registered before the command is submitted and retired only after
        its result comes back, so the Agent actor asking at dispatch time
        always gets the true answer.  Used only to split Agent-actor cost by
        origin for the calibration contract.
        """

        with self._effect_lock:
            return correlation_id in self._agent_attempts

    def submit(self, command: object) -> PortAdmission:
        admission = self._runtime.tell(self._handle, command)
        if admission is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        port_admission = (
            PortAdmission.OVERLOADED
            if admission is AdmissionResult.OVERLOADED
            else PortAdmission.CLOSING
        )
        _publish(
            self._events,
            PortCommandRejected(
                correlation_id=str(getattr(command, "correlation_id", "")),
                domain="session",
                generation=self.generation,
                version=self.version,
                code=(
                    "PORT_OVERLOADED"
                    if port_admission is PortAdmission.OVERLOADED
                    else "PORT_CLOSING"
                ),
                detail=f"session command admission is {port_admission.value}",
                admission=port_admission,
            ),
        )
        return port_admission

    def accept_agent_event(self, event: AgentEvent) -> bool:
        """Accept one correlated Agent result from the composition fan-out."""

        if not isinstance(event, (AgentMutationCompleted, PortCommandRejected)):
            return False
        if isinstance(event, PortCommandRejected):
            if event.domain != "agent" or event.admission is not None:
                return False
        with self._effect_lock:
            attempt = self._agent_attempts.get(event.correlation_id)
        if attempt is None:
            return False
        effect_id, custody_token = attempt
        delivered = self._deliver_internal(
            _AgentEffectResult(effect_id, custody_token, event)
        )
        if not delivered:
            self._submission_failed(effect_id, custody_token)
        return delivered

    def reconcile_pending_effects(self) -> int:
        """Explicit Agent-recovery nudge; Session restarts replay automatically."""

        replayed = self._reconcile_pending_effects(expected_generation=None)
        return 0 if replayed is None else replayed.submitted

    def _reconcile_pending_effects(
        self, *, expected_generation: int | None
    ) -> _ReplayScan | None:
        if expected_generation is not None:
            snapshot = self._runtime.snapshot(self._handle)
            if (
                snapshot.generation != expected_generation
                or snapshot.state is not ActorState.RUNNING
            ):
                return None

        effects = self._store.load().pending_session_agent_effects
        if expected_generation is not None:
            snapshot = self._runtime.snapshot(self._handle)
            if (
                snapshot.generation != expected_generation
                or snapshot.state is not ActorState.RUNNING
            ):
                return None
        generation = (
            expected_generation
            if expected_generation is not None
            else self._runtime.snapshot(self._handle).generation
        )
        submitted = 0
        blocked = False
        for effect in effects:
            with self._effect_lock:
                surviving = self._effect_custody.get(effect.effect_id)
            if surviving is not None:
                blocked = True
                continue
            # The first snapshot may have been taken while a surviving worker
            # was completing.  Successful completion removes the durable row
            # before releasing its custody token; once we observe no token, a
            # second durable read closes that handoff race and prevents replay
            # from enqueuing a row that was just retired.
            if not any(
                current.effect_id == effect.effect_id
                for current in self._store.load().pending_session_agent_effects
            ):
                continue
            if self._request_effect(
                effect, generation, StateCostOrigin.BACKGROUND
            ):
                submitted += 1
                continue
            with self._effect_lock:
                blocked = blocked or effect.effect_id in self._effect_custody
        return _ReplayScan(submitted, blocked)

    def _schedule_generation_replay(self, generation: int) -> None:
        threading.Thread(
            target=self._replay_when_running,
            args=(generation,),
            name=f"hyprial-session-replay-{generation}",
            daemon=True,
        ).start()

    def _replay_when_running(self, generation: int) -> None:
        while not self._draining:
            try:
                snapshot = self._runtime.snapshot(self._handle)
            except (KeyError, RuntimeError):
                time.sleep(0.005)
                continue
            if snapshot.generation > generation:
                return
            if snapshot.state in {ActorState.QUARANTINED, ActorState.STOPPED}:
                return
            if snapshot.generation != generation or snapshot.state is not ActorState.RUNNING:
                time.sleep(0.005)
                continue
            with self._replay_lock:
                if generation in self._replayed_generations:
                    return
            try:
                replayed = self._reconcile_pending_effects(
                    expected_generation=generation
                )
            except Exception:
                # Desired-state I/O can be the reason the previous generation
                # failed.  Keep this generation's restart hook alive; durable
                # custody makes retry safe and the generation fence prevents a
                # delayed hook from acting after a subsequent crash.
                with self._replay_lock:
                    attempted = self._replay_attempted.get(generation)
                if attempted is not None:
                    attempted.set()
                time.sleep(0.01)
                continue
            with self._replay_lock:
                attempted = self._replay_attempted.get(generation)
            if attempted is not None:
                attempted.set()
            if replayed is None:
                continue
            if replayed.blocked_by_surviving_custody:
                time.sleep(0.005)
                continue
            with self._replay_lock:
                self._replayed_generations.add(generation)
            return

    def read_session(self, actor: str) -> SessionProjection | None:
        sessions, version = self._session_projection.read()
        session = next(
            (
                item
                for item in sessions
                if item.actor == actor
            ),
            None,
        )
        return None if session is None else _projection(session, version)

    def read_sessions(self) -> tuple[SessionProjection, ...]:
        sessions, version = self._session_projection.read()
        return tuple(
            _projection(session, version)
            for session in sessions
        )

    def read_runtime(self, actor: str) -> SessionRuntimeProjection | None:
        sessions, _version = self._session_projection.read()
        session = next(
            (item for item in sessions if item.actor == actor),
            None,
        )
        if session is None:
            return None
        return self._runtime_projection.read(
            actor,
            session.session_ref,
            now_ms=self._clock_ms(),
            ttl_seconds=self._lease_ttl_seconds,
        )

    def assert_owner(self, actor: str, session_ref: str) -> None:
        sessions, _version = self._session_projection.read()
        current = next((item for item in sessions if item.actor == actor), None)
        if current is not None and current.session_ref == session_ref:
            return
        if current is not None:
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        relocated = next(
            (item for item in sessions if item.session_ref == session_ref), None
        )
        if relocated is not None:
            if owner_only_relocation(actor, relocated.actor):
                # Owner migration moved the same session's address; not a
                # supersede (mirrors _owned_session's relocated branch).
                return
            raise SessionOwnershipError(
                ipc_errors.SESSION_SUPERSEDED,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        raise SessionOwnershipError(
            ipc_errors.STALE_SESSION,
            f"interactive session {session_ref} no longer owns actor {actor}",
        )

    def drain(self, timeout: float = 5.0) -> DrainReport:
        started = time.monotonic()
        self._draining = True
        persistence = self._persistence
        persistence_complete = (
            True if persistence is None else persistence.close(timeout / 3)
        )
        worker_complete = self._effect_worker.drain(timeout / 3)
        remaining = max(0.0, timeout - (time.monotonic() - started))
        report = self._runtime.drain(remaining)
        if worker_complete and persistence_complete:
            return DrainReport(
                complete=report.complete,
                elapsed=time.monotonic() - started,
                remaining=report.remaining,
            )
        return DrainReport(
            complete=False,
            elapsed=time.monotonic() - started,
            remaining=(self._handle,),
        )

    def _request_effect(
        self,
        effect: PendingSessionAgentEffect,
        generation: int,
        cost_origin: StateCostOrigin,
    ) -> bool:
        custody_token = uuid.uuid4().hex
        agent_correlation_id = f"{effect.effect_id}.{custody_token}"
        custody = _EffectCustody(
            custody_token, generation, agent_correlation_id, cost_origin
        )
        with self._effect_lock:
            if self._draining or effect.effect_id in self._effect_custody:
                return False
            self._effect_custody[effect.effect_id] = custody
            self._agent_attempts[agent_correlation_id] = (
                effect.effect_id,
                custody_token,
            )
        accepted = self._effect_worker.submit(
            effect,
            custody_token=custody.token,
            generation=generation,
            agent_correlation_id=agent_correlation_id,
        )
        if not accepted:
            self._submission_failed(effect.effect_id, custody.token)
        return accepted

    def _submission_failed(self, effect_id: str, custody_token: str) -> None:
        with self._effect_lock:
            custody = self._effect_custody.get(effect_id)
            if custody is not None and custody.token == custody_token:
                self._agent_attempts.pop(custody.agent_correlation_id, None)
                self._effect_custody.pop(effect_id, None)

    def _retire_effect(self, effect_id: str, custody_token: str) -> None:
        with self._effect_lock:
            custody = self._effect_custody.get(effect_id)
            if custody is not None and custody.token == custody_token:
                self._agent_attempts.pop(custody.agent_correlation_id, None)
                self._effect_custody.pop(effect_id, None)

    def _effect_cost_origin(
        self, effect_id: str, custody_token: str
    ) -> StateCostOrigin:
        with self._effect_lock:
            custody = self._effect_custody.get(effect_id)
            return (
                custody.cost_origin
                if custody is not None and custody.token == custody_token
                else StateCostOrigin.BACKGROUND
            )

    def _deliver_internal(self, command: object) -> bool:
        deadline = time.monotonic() + 1.0
        delay = 0.001
        while True:
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                return True
            if admission is AdmissionResult.CLOSED:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)


__all__ = ["SessionActor", "SessionOwnershipError"]
