"""Interactive session routes: effects, leases, expiry, persona routes and the session route coordinator."""

from __future__ import annotations

from __future__ import annotations
import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.network.routing.session_route_coordinator  import (
    SessionRouteCoordinator,
    SessionRouteKind,
    SessionRefRetirement,
    SessionRouteRequest,
    SessionRouteOverloaded,
)
from hyprial.identity import (
    AGENT_HEARTBEAT_TTL_SECONDS,
)
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.transport import (
    KeySpace,
)
from hyprial.daemon.impl.desired_state  import (
    InteractiveSession,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.network.route_ports  import DropRouteCommand, EnsureRouteCommand, RouteSpec
from hyprial.daemon.impl.route_registration  import (
    RouteCommandError,
    RouteRegistrationClient,
)
from hyprial.daemon.impl.operations.session_ports  import (
    SessionProjection,
    UnregisterSessionCommand,
)
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
)
from hyprial.kernel import (
    TARGET_KIND_AGENT,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.actors.sessions.registry import (
    _session_harness,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
)


class _InteractiveRouteEffectGate:
    """Reference-counted per-actor exclusion for route I/O phases."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0

_INTERACTIVE_HEARTBEAT_TTL_SECONDS = AGENT_HEARTBEAT_TTL_SECONDS


class _SessionRoutesMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ensure_session_route_coordinator(self) -> SessionRouteCoordinator:
        coordinator = self._session_route_coordinator
        if coordinator is None:
            coordinator = SessionRouteCoordinator(self._apply_session_route_request)
            self._session_route_coordinator = coordinator
        return coordinator

    def _restore_persona_routes(self) -> None:
        """(Re)declare daemon-backed presence for every registered agent."""

        for agent in self.agents.list():
            self._declare_persona_route(agent.uri)

    def _declare_persona_route(self, actor_uri: str) -> None:
        """Acquire the durable persona lease on the one physical route."""

        routes = self._routes
        if routes is None:
            return
        routes.ensure(
            self._actor_route_spec(actor_uri),
            owner_lease=f"persona:{actor_uri}",
        )

    def _drop_persona_route(self, actor_uri: str) -> None:
        routes = self._routes
        if routes is None:
            return
        routes.drop(actor_uri, owner_lease=f"persona:{actor_uri}")

    def _call_session_route(
        self,
        kind: SessionRouteKind,
        *,
        actor: str,
        session_ref: str | None,
        command: object | None = None,
        retirement: SessionRefRetirement | None = None,
    ) -> object:
        coordinator = self._session_route_coordinator
        if coordinator is None:
            return self._apply_session_route_request(
                SessionRouteRequest(
                    uuid4().hex,
                    0,
                    kind,
                    actor,
                    session_ref,
                    command,  # type: ignore[arg-type]
                    retirement=retirement,
                )
            )
        try:
            return coordinator.call(
                kind, actor=actor, session_ref=session_ref,
                session_command=command, retirement=retirement, timeout=65.0,
            )
        except SessionRouteOverloaded as error:
            raise ipc_errors.DaemonUnavailableError(str(error)) from error

    def _apply_session_route_request(self, request: SessionRouteRequest) -> object:
        kind = request.kind
        actor = request.actor
        session_ref = request.session_ref
        if kind is SessionRouteKind.EXPIRE:
            self._expire_stale_channel_routes_owned(
                request.observed_at if request.observed_at is not None else self._clock(),
                operation_id=request.operation_id,
                generation=request.epoch,
            )
            return None
        if kind is SessionRouteKind.ENSURE:
            with self._interactive_route_effect(actor):
                return self._ensure_interactive_route_effect(
                    actor,
                    session_ref,
                    operation_id=request.operation_id,
                    generation=request.epoch,
                    version=0,
                )
        if kind is SessionRouteKind.DROP:
            with self._interactive_route_effect(actor):
                self._close_interactive_route_effect(
                    actor,
                    session_ref,
                    operation_id=request.operation_id,
                    generation=request.epoch,
                    version=0,
                )
            return None
        if kind is SessionRouteKind.RETIRE:
            retirement = request.retirement
            if retirement is None:
                raise TypeError("session retirement requires its destroy fence")
            reservation = self._reserve_agent_destroy(actor)
            try:
                current_agent = self.agents.get(actor)
                if (
                    current_agent is None
                    or current_agent.entity_token != retirement.entity_token
                ):
                    return reservation
                current = self._agent_session_domains.session.read_session(actor)
                refs = set(retirement.session_refs)
                if current is not None and current.session_ref is not None:
                    refs.add(current.session_ref)
                worker_session_ref = self._worker_session_ref(actor)
                if worker_session_ref is not None:
                    refs.add(worker_session_ref)
                self.agents.retire_session_refs(
                    actor,
                    retirement.entity_token,
                    tuple(sorted(refs)),
                    reason=retirement.reason,
                    destroy_attempt=reservation.reservation_token,
                )
            except BaseException:
                self._release_agent_destroy_reservation(
                    reservation.reservation_token
                )
                raise
            return reservation
        command = request.session_command
        if command is None:
            raise TypeError("session route mutation requires a session command")
        if kind in {
            SessionRouteKind.REGISTER,
            SessionRouteKind.REFRESH,
            SessionRouteKind.HEARTBEAT,
        }:
            # This check is deliberately inside the same per-actor coordinator
            # operation as the session write. A destroy's RETIRE operation
            # therefore linearizes wholly before or after registration; there
            # is no check-then-register gap for an old ref to cross.
            assert session_ref is not None
            self._reject_retired_session_ref(actor, session_ref)
        if kind is SessionRouteKind.REGISTER:
            completed = self._call_session(command)
            result_actor = completed.result.actor
            agent = self.agents.get(result_actor)
            if agent is not None:
                self.agents.record_session_ref(
                    result_actor, agent.entity_token, session_ref
                )
            with self._interactive_route_effect(result_actor):
                with self._interactive_route_lock:
                    accepted = self._accept_interactive_route_observation_locked(
                        result_actor, session_ref, completed.version
                    )
                if accepted:
                    self._close_other_interactive_routes_effect(
                        result_actor,
                        session_ref,
                        operation_id=f"{request.operation_id}:supersede-current",
                        generation=request.epoch,
                        version=completed.version,
                    )
                    self._ensure_interactive_route_effect(
                        result_actor,
                        session_ref,
                        operation_id=f"{request.operation_id}:register",
                        generation=request.epoch,
                        version=completed.version,
                    )
                    with self._interactive_route_lock:
                        self._retire_interactive_route_versions_locked(
                            result_actor,
                            keep_session_ref=session_ref,
                            keep=True,
                        )
            for superseded_actor in completed.result.superseded_actors:
                with self._interactive_route_effect(superseded_actor):
                    with self._interactive_route_lock:
                        accepted = self._accept_interactive_route_observation_locked(
                            superseded_actor, session_ref, completed.version
                        )
                    if accepted:
                        self._close_interactive_route_effect(
                            superseded_actor,
                            None,
                            operation_id=(
                                f"{request.operation_id}:superseded:"
                                f"{superseded_actor}"
                            ),
                            generation=request.epoch,
                            version=completed.version,
                        )
            return completed
        completed = self._call_session(command)
        if kind in {SessionRouteKind.REFRESH, SessionRouteKind.HEARTBEAT}:
            result_actor = completed.result.actor
            with self._interactive_route_effect(result_actor):
                with self._interactive_route_lock:
                    accepted = self._accept_interactive_route_observation_locked(
                        result_actor, session_ref, completed.version
                    )
                if accepted:
                    self._ensure_interactive_route_effect(
                        result_actor,
                        session_ref,
                        operation_id=f"{request.operation_id}:{kind.value}",
                        generation=request.epoch,
                        version=completed.version,
                    )
        elif kind is SessionRouteKind.UNREGISTER:
            result_actor = completed.result.actor
            with self._interactive_route_effect(result_actor):
                with self._interactive_route_lock:
                    accepted = (
                        completed.result.unregistered
                        and self._accept_interactive_route_observation_locked(
                            result_actor, session_ref, completed.version
                        )
                    )
                if accepted:
                    self._close_interactive_route_effect(
                        result_actor,
                        session_ref,
                        operation_id=f"{request.operation_id}:unregister",
                        generation=request.epoch,
                        version=completed.version,
                    )
        else:
            raise TypeError(f"unsupported session route operation {kind}")
        return completed

    def _ensure_interactive_route(
        self, actor: str, session_ref: str | None = None
    ) -> bool:
        if self._session_route_coordinator is not None:
            return bool(self._call_session_route(
                SessionRouteKind.ENSURE, actor=actor, session_ref=session_ref,
            ))
        with self._interactive_route_effect(actor):
            return self._ensure_interactive_route_effect(
                actor,
                session_ref,
                operation_id=f"session-route:ensure:{uuid4().hex}",
                generation=0,
                version=0,
            )

    def _accept_interactive_route_observation_locked(
        self, actor: str, session_ref: str | None, version: int
    ) -> bool:
        """Fence route work derived from a Session-domain observation.

        Session mutation results and session snapshots share one monotonic
        version.  The route lock compares it with both the actor and exact
        ``(actor, session_ref)`` high-water marks before the matching
        ensure/drop.  The actor mark covers last-writer-wins registration;
        the exact mark covers delayed observations of one lease.  An older
        result therefore cannot restore or revoke a newer route.
        """

        key = (actor, session_ref)
        latest_actor = self._interactive_route_actor_versions.get(actor, -1)
        latest_session = self._interactive_route_versions.get(key, -1)
        if version < max(latest_actor, latest_session):
            return False
        self._interactive_route_actor_versions[actor] = version
        self._interactive_route_versions[key] = version
        return True

    def _retire_interactive_route_versions_locked(
        self,
        actor: str,
        *,
        keep_session_ref: str | None = None,
        keep: bool = False,
    ) -> None:
        for key in tuple(self._interactive_route_versions):
            if key[0] == actor and (not keep or key[1] != keep_session_ref):
                del self._interactive_route_versions[key]

    @contextlib.contextmanager
    def _interactive_route_effect(self, actor: str) -> Iterator[None]:
        with self._interactive_route_lock:
            gate = self._interactive_route_effect_gates.get(actor)
            if gate is None:
                gate = _InteractiveRouteEffectGate()
                self._interactive_route_effect_gates[actor] = gate
            gate.users += 1
        gate.lock.acquire()
        try:
            yield
        finally:
            gate.lock.release()
            with self._interactive_route_lock:
                gate.users -= 1
                if (
                    gate.users == 0
                    and self._interactive_route_effect_gates.get(actor) is gate
                ):
                    del self._interactive_route_effect_gates[actor]

    @contextlib.contextmanager
    def _try_interactive_route_effect(self, actor: str) -> Iterator[bool]:
        with self._interactive_route_lock:
            gate = self._interactive_route_effect_gates.get(actor)
            if gate is None:
                gate = _InteractiveRouteEffectGate()
                self._interactive_route_effect_gates[actor] = gate
            gate.users += 1
        acquired = gate.lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                gate.lock.release()
            with self._interactive_route_lock:
                gate.users -= 1
                if (
                    gate.users == 0
                    and self._interactive_route_effect_gates.get(actor) is gate
                ):
                    del self._interactive_route_effect_gates[actor]

    def _ensure_interactive_route_effect(
        self,
        actor: str,
        session_ref: str | None = None,
        *,
        operation_id: str,
        generation: int,
        version: int,
    ) -> bool:
        """Publish and receive for a CC actor distinct from this daemon node."""

        if actor == self.node_id:
            return False
        routes = self._routes
        if routes is None:
            return False
        lease = self._interactive_route_lease(actor, session_ref)
        canonical = classify_target_identity(actor) == TARGET_KIND_AGENT
        completed = self._settled_route_mutation(
            routes,
            operation_id=operation_id,
            generation=generation,
            version=version,
            actor=actor,
            owner_lease=lease,
            ensure_spec=self._actor_route_spec(actor, advertise=canonical),
        )
        if not canonical:
            # Admission gate, downgrade-don't-reject: a pre-canonical
            # desired-state session can still carry a bare actor name.  The
            # inbox endpoint stays open so in-flight and locally addressed
            # messages keep landing, but a non-canonical actor is never
            # advertised into the agent liveliness keyspace again -- that
            # keyspace is what `targets` reads, and a bare name there is
            # indistinguishable from a node's self-token.
            self._log(
                "warn",
                "daemon",
                "daemon.interactive_route.not_advertised",
                actor=actor,
                targetKind=classify_target_identity(actor),
                detail=(
                    "non-canonical interactive actor receives but is not "
                    "advertised as an agent"
                ),
            )
        return completed.changed

    def _restore_interactive_routes(self) -> None:
        for session in self._agent_session_domains.session.read_sessions():
            # Persisted Channel state is recovery intent, never liveness. The
            # surviving child reopens its route via lease-matched refresh.
            if session.source != "claude-channel":
                self._ensure_interactive_route(session.actor, session.session_ref)

    def _clean_dead_interactive_sessions(self) -> None:
        """Retire only sessions whose PID/birth fence proves the carrier dead."""

        from hyprial.daemon.impl.mcp.channel.ownership import _owner_process_status

        self._agent_recovery_cleanups.clear()
        sessions = self._agent_session_domains.session.read_sessions()
        for session in sessions:
            if (
                session.source != "codex-app-server"
                or session.process_pid is None
                or session.process_identity is None
            ):
                continue
            verdict = _owner_process_status(
                session.process_pid, session.process_identity
            ).value
            if verdict not in {"pid-missing", "identity-mismatch"}:
                continue
            if session.session_ref is None:
                continue
            completed = self._call_session(
                UnregisterSessionCommand(
                    correlation_id=f"session:recovery-cleanup:{uuid4().hex}",
                    actor=session.actor,
                    session_ref=session.session_ref,
                    manage_agent=self.agents.get(session.actor) is not None,
                )
            )
            if not completed.result.unregistered:
                continue
            cleanup: JsonObject = {
                "source": session.source,
                "harness": _session_harness(session),
                "runtime": session.runtime,
                "sessionId": session.session_ref,
                "processPid": session.process_pid,
                "liveness": verdict,
            }
            self._agent_recovery_cleanups[session.actor] = cleanup
            self._log(
                "info",
                "agents",
                "agent.recovery.cleaned",
                actor=session.actor,
                source=session.source,
                harness=_session_harness(session),
                runtime=session.runtime,
                sessionId=session.session_ref,
                processPid=session.process_pid,
                liveness=verdict,
                detail=(
                    "interactive session registration removed before route and "
                    "binding recovery because PID plus birth identity proved "
                    "the exact carrier process is dead"
                ),
            )

    def _expire_stale_channel_routes(
        self, now: float, sessions: tuple[SessionProjection, ...] | None = None
    ) -> None:
        coordinator = self._session_route_coordinator
        if coordinator is not None:
            admission, _operation_id = coordinator.request(
                SessionRouteKind.EXPIRE, observed_at=now
            )
            if admission is AdmissionResult.OVERLOADED:
                self._log("warn", "daemon", "session_route.expiry_overloaded")
            return
        self._expire_stale_channel_routes_owned(now, sessions)

    def _expire_stale_channel_routes_owned(
        self,
        now: float,
        sessions: tuple[SessionProjection, ...] | None = None,
        *,
        operation_id: str | None = None,
        generation: int = 0,
    ) -> None:
        # Reads can touch durable projections, so observe outside the route
        # lock. Only the version comparison and route mutation are serialized.
        observed_sessions = (
            self._agent_session_domains.session.read_sessions()
            if sessions is None
            else sessions
        )
        observations = tuple(
            (session, self._channel_alive(session, now))
            for session in observed_sessions
            if session.source == "claude-channel"
            and session.channel_lease_backed
        )
        root_operation = operation_id or f"session-route:expire:{uuid4().hex}"
        pending = [session for session, alive in observations if not alive]
        while pending:
            retained: list[SessionProjection] = []
            progressed = False
            for session in pending:
                with self._try_interactive_route_effect(session.actor) as acquired:
                    if not acquired:
                        retained.append(session)
                        continue
                    progressed = True
                    with self._interactive_route_lock:
                        accepted = self._accept_interactive_route_observation_locked(
                            session.actor, session.session_ref, session.version
                        )
                    if accepted:
                        self._close_interactive_route_effect(
                            session.actor,
                            session.session_ref,
                            operation_id=(
                                f"{root_operation}:{session.actor}:"
                                f"{session.session_ref or 'legacy'}"
                            ),
                            generation=generation,
                            version=session.version,
                        )
            pending = retained
            if pending and not progressed:
                time.sleep(0.005)

    def _channel_alive(self, session: InteractiveSession, now: float) -> bool:
        del now
        runtime = self._agent_session_domains.session.read_runtime(session.actor)
        lease_backed = bool(
            getattr(
                session,
                "channel_lease_backed",
                getattr(session, "channel_lease_digest", None) is not None,
            )
        )
        return bool(
            lease_backed
            and runtime is not None
            and runtime.current_epoch == self.epoch
            and runtime.alive
        )

    def _close_interactive_route(
        self, actor: str, session_ref: str | None = None
    ) -> None:
        if self._session_route_coordinator is not None:
            self._call_session_route(
                SessionRouteKind.DROP, actor=actor, session_ref=session_ref,
            )
            return
        with self._interactive_route_effect(actor):
            self._close_interactive_route_effect(
                actor,
                session_ref,
                operation_id=f"session-route:drop:{uuid4().hex}",
                generation=0,
                version=0,
            )

    def _close_other_interactive_routes_effect(
        self,
        actor: str,
        session_ref: str,
        *,
        operation_id: str,
        generation: int,
        version: int,
    ) -> None:
        routes = self._routes
        if routes is None:
            return
        current_owner = self._interactive_route_lease(actor, session_ref)
        for index, owner in enumerate(tuple(routes.owners(actor))):
            if owner.startswith("session:") and owner != current_owner:
                self._settled_route_mutation(
                    routes,
                    operation_id=f"{operation_id}:{index}",
                    generation=generation,
                    version=version,
                    actor=actor,
                    owner_lease=owner,
                )

    def _close_interactive_route_effect(
        self,
        actor: str,
        session_ref: str | None = None,
        *,
        operation_id: str,
        generation: int,
        version: int,
    ) -> None:
        routes = self._routes
        if routes is None:
            with self._interactive_route_lock:
                if session_ref is None:
                    self._retire_interactive_route_versions_locked(actor)
                else:
                    self._interactive_route_versions.pop((actor, session_ref), None)
            return
        if session_ref is not None:
            owners = (self._interactive_route_lease(actor, session_ref),)
        else:
            owners = tuple(
                owner
                for owner in routes.owners(actor)
                if owner.startswith("session:")
            )
        for index, owner in enumerate(owners):
            self._settled_route_mutation(
                routes,
                operation_id=f"{operation_id}:{index}",
                generation=generation,
                version=version,
                actor=actor,
                owner_lease=owner,
            )
        with self._interactive_route_lock:
            if session_ref is None:
                self._retire_interactive_route_versions_locked(actor)
            else:
                self._interactive_route_versions.pop((actor, session_ref), None)

    def _settled_route_mutation(
        self,
        routes: RouteRegistrationClient,
        *,
        operation_id: str,
        generation: int,
        version: int,
        actor: str,
        owner_lease: str,
        ensure_spec: RouteSpec | None = None,
    ) -> Any:
        if not isinstance(routes, RouteRegistrationClient):
            if ensure_spec is not None:
                return routes.ensure(ensure_spec, owner_lease=owner_lease)
            return routes.drop(actor, owner_lease=owner_lease)
        key = (actor, owner_lease)
        with self._interactive_route_lock:
            expected_token = self._interactive_route_resource_tokens.get(key)
        attempt_token = f"{operation_id}:attempt:0"
        correlation_id = f"{attempt_token}:completion"
        payload = (
            EnsureRouteCommand(
                correlation_id,
                attempt_token,
                generation,
                version,
                ensure_spec,
                owner_lease,
            )
            if ensure_spec is not None
            else DropRouteCommand(
                correlation_id,
                attempt_token,
                generation,
                version,
                actor,
                owner_lease,
            )
        )
        request = LifecycleMutationRequest(
            correlation_id,
            attempt_token,
            operation_id,
            expected_token if ensure_spec is None else None,
            payload,
        )
        retry = 0
        while True:
            try:
                completed = routes.call_settled(request)
                break
            except RouteCommandError as error:
                if error.code == "PORT_CLOSING":
                    raise
                self._log(
                    "warn",
                    "daemon",
                    "session_route.route_retry",
                    actor=actor,
                    ownerLease=owner_lease,
                    operationId=operation_id,
                    attemptToken=attempt_token,
                    retry=retry,
                    errorCode=error.code,
                    detail=error.detail,
                )
                retry += 1
                time.sleep(min(0.005 * (2 ** min(retry, 7)), 0.5))
        token = completed.provenance.resource_token
        with self._interactive_route_lock:
            if ensure_spec is not None:
                self._interactive_route_resource_tokens[key] = token
            elif expected_token is None or token == expected_token:
                self._interactive_route_resource_tokens.pop(key, None)
            else:
                self._interactive_route_resource_tokens[key] = token
        return completed

    @staticmethod
    def _interactive_route_lease(actor: str, session_ref: str | None) -> str:
        return f"session:{session_ref or f'legacy:{actor}'}"

    @staticmethod
    def _actor_route_spec(actor: str, *, advertise: bool = True) -> RouteSpec:
        keys = KeySpace()
        return RouteSpec(
            route_id=actor,
            liveliness_key=keys.actor_liveliness(actor),
            inbox_key=keys.inbox_all(actor),
            advertise=advertise,
        )
