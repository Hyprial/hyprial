"""Lifecycle manager start/step execution and the lifecycle.start/down IPC families."""

from __future__ import annotations

from __future__ import annotations
import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.identity import (
    AgentError,
    HandoverNotice,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyFacade,
    LocalFirstDeliveryTransport,
    ZenohInboxEndpoint,
)
from hyprial.daemon.impl.transport import (
    KeySpace,
)
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition  import (
    HarnessPortClient,
    harness_launch_projection,
)
from hyprial.daemon.impl.lifecycle.atomic_lifecycle_ports  import (
    AtomicLifecycleDomainPort,
    HarnessLifecycleDomainPort,
)
from hyprial.daemon.impl.correlation.correlation  import CorrelationEventRouter
from hyprial.kernel import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleKind,
    LifecycleOperation,
    LifecyclePorts,
    LifecycleSpec,
    LifecycleState,
)
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator  import LifecycleCoordinator
from hyprial.daemon.impl.network.route_ports  import RouteSpec
from hyprial.daemon.impl.route_registration  import (
    RoutePreparationFailed,
    RouteRegistrationClient,
    RouteRegistrationIo,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.wiring.transport import (
    _HarnessActorRegistration,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


class _LifecycleOpsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _start_lifecycle_manager(
        self,
        *,
        transport: TransportSessionAuthority,
        inbox: DeliveryCustodyFacade,
        local_delivery: LocalFirstDeliveryTransport,
        harnesses: HarnessPortClient,
    ) -> None:
        """Compose the durable cross-domain lifecycle control plane."""

        router = CorrelationEventRouter()

        agent_port = AtomicLifecycleDomainPort(
            domain="agent",
            router=router,
            generation=lambda: self._agent_session_domains.agent.generation,
            version=lambda: self._agent_session_domains.agent.version,
            submit_domain=self._agent_session_domains.agent.submit,
            wait_domain=self._agent_session_domains.wait_agent_lifecycle,
            retire_receipt=(
                self._agent_session_domains.retire_agent_lifecycle_receipt
            ),
            confirm_receipt_retired=(
                self._agent_session_domains.confirm_agent_lifecycle_receipt
            ),
        )
        session_port = AtomicLifecycleDomainPort(
            domain="session",
            router=router,
            generation=lambda: self._agent_session_domains.session.generation,
            version=lambda: self._agent_session_domains.session.version,
            submit_domain=self._agent_session_domains.session.submit,
            wait_domain=self._agent_session_domains.wait_session_lifecycle,
            retire_receipt=self._agent_session_domains.retire_session_lifecycle_receipt,
            confirm_receipt_retired=(
                self._agent_session_domains.confirm_session_lifecycle_receipt
            ),
        )
        harness_port = HarnessLifecycleDomainPort(
            failure_control=harnesses.fail_lifecycle,
            domain="harness",
            router=router,
            generation=lambda: harnesses.generation,
            version=lambda: harnesses.version,
            submit_domain=harnesses.submit_lifecycle,
            wait_domain=harnesses.wait_lifecycle,
            retire_receipt=harnesses.retire_lifecycle_receipt,
            confirm_receipt_retired=harnesses.confirm_lifecycle_receipt_retired,
            release_replay_claim=harnesses.actor.release_lifecycle_replay,
        )

        def register_route(spec: RouteSpec) -> tuple[Any, Any]:
            local = local_delivery.register_actor(spec.route_id)
            combined: _HarnessActorRegistration | None = None
            try:
                network = (
                    transport.declare_liveliness(spec.liveliness_key)
                    if spec.advertise
                    else None
                )
                combined = _HarnessActorRegistration(
                    local,
                    network,
                    actor_uri=spec.route_id,
                    layer="lifecycle-route",
                    event_sink=lambda level, event, **fields: self._log(
                        level, "daemon", event, **fields
                    ),
                )
                try:
                    endpoint = ZenohInboxEndpoint(
                        transport,
                        inbox,
                        spec.route_id,
                        declare_receipts=False,
                    )
                except BaseException as prepare_error:
                    try:
                        combined.close(
                            reason="route-endpoint-start-failed",
                            initiator="lifecycle-manager",
                        )
                    except BaseException as cleanup_error:
                        raise RoutePreparationFailed(
                            f"{type(prepare_error).__name__}: {prepare_error}; "
                            "compound rollback remains pending: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}",
                            liveliness=combined,
                            inbox_closed=True,
                        ) from cleanup_error
                    raise
                return combined, endpoint
            except RoutePreparationFailed:
                raise
            except BaseException as prepare_error:
                if combined is None:
                    try:
                        local.close()
                    except BaseException as cleanup_error:
                        raise RoutePreparationFailed(
                            f"{type(prepare_error).__name__}: {prepare_error}; "
                            "local rollback remains pending: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}",
                            liveliness=local,
                            inbox_closed=True,
                        ) from cleanup_error
                raise

        route_port = RouteRegistrationIo(
            transport,
            lambda _route_id: lambda _selector: b"ok",
            router,
            registration_factory=register_route,
        )
        manager = LifecycleCoordinator(
            self.state_db,
            LifecyclePorts(agent_port, session_port, harness_port, route_port),
            router,
            event_sink=self._lifecycle_thread_event,
            live_resource=self._harness_resource_is_live,
            journal_store=self._state_persistence.settled_journal,
            combined_shared_receipts=True,
        )
        self._lifecycle_router = router
        self._lifecycle_domain_ports = (agent_port, session_port, harness_port)
        self._route_registration = route_port
        self._routes = RouteRegistrationClient(route_port, router)
        self._lifecycle_manager = manager

    def _lifecycle_spec(self, spec: HarnessLaunchSpec) -> LifecycleSpec:
        actor = self._canonical_harness_uri(spec.name, spec)
        keys = KeySpace()
        return LifecycleSpec(
            agent_name=spec.name,
            actor=actor,
            harness=harness_launch_projection(spec),
            route=RouteSpec(
                route_id=actor,
                liveliness_key=keys.actor_liveliness(actor),
                inbox_key=keys.inbox_all(actor),
            ),
        )

    def _run_lifecycle_operation(
        self,
        operation: LifecycleOperation,
        *,
        # Outlast the manager's operation deadline so this wait receives the
        # terminal FAILED it writes, rather than timing out first (card 104164aa
        # (c)).
        timeout: float = (
            LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS
        ),
    ) -> Any:
        manager = self._lifecycle_manager
        if manager is None:
            raise DaemonRequestError(
                ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE,
                "lifecycle manager is not running",
            )
        # Instrumentation for a failure that is otherwise unobservable.  The
        # caller's error carries an operation id but no elapsed and no budget,
        # so an operator reading logs/daemon.jsonl after the fact cannot tell a
        # 3-second refusal from a deadline that expired at 80 seconds
        # (2026-09-22: neither the settle timeout nor the admission refusal
        # wrote anything at all).
        operation_id = operation.operation_id
        kind = operation.kind.value
        budget_ms = int(timeout * 1000)
        started = time.monotonic()
        self._log_lifecycle_operation(
            "info",
            "daemon.lifecycle.operation.submitted",
            operationId=operation_id,
            kind=kind,
            budgetMs=budget_ms,
        )
        admission = manager.submit(operation)
        if admission is not PortAdmission.ACCEPTED:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.refused",
                operationId=operation_id,
                kind=kind,
                admission=admission.value,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                (
                    ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE
                    if manager.crashed
                    else ipc_errors.DAEMON_START_FAILED
                ),
                f"lifecycle operation admission: {admission.value}",
            )
        try:
            result = manager.wait(operation_id, timeout)
        except TimeoutError as error:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.unsettled",
                operationId=operation_id,
                kind=kind,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                ipc_errors.LIFECYCLE_OPERATION_UNSETTLED,
                f"lifecycle operation did not settle: {operation_id}",
            ) from error
        if result.state is not LifecycleState.COMPLETED:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.failed",
                operationId=operation_id,
                kind=kind,
                state=result.state.value,
                errorCode=result.error_code,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                result.error_code or ipc_errors.DAEMON_START_FAILED,
                result.error
                or f"lifecycle operation ended in {result.state.value}",
            )
        self._log_lifecycle_operation(
            "info",
            "daemon.lifecycle.operation.settled",
            operationId=operation_id,
            kind=kind,
            elapsedMs=int((time.monotonic() - started) * 1000),
        )
        if operation.kind in {LifecycleKind.CREATE, LifecycleKind.TRANSFER}:
            launch = operation.target.harness
            if launch.harness != "lark":
                self._ensure_agent(
                    operation.target.actor,
                    harness=launch.harness,
                    interactive=operation.target.session is not None,
                    cwd=launch.cwd,
                    args=launch.args,
                    provider=launch.model_provider,
                    model=launch.model,
                )
        return result

    def _harness_resource_is_live(self, resource_key: str, token: str) -> bool:
        """Whether ``token`` is the active token of ``resource_key`` right now."""

        return any(
            resource.resource_key == resource_key
            and resource.active
            and resource.resource_token == token
            for resource in self.desired_state.load().lifecycle_resources
        )

    def _lifecycle_thread_event(self, event: str, **fields: Any) -> None:
        """Surface lifecycle consumer-thread faults on the daemon event log.

        Both names are runtime-thread events, not startup failures.  They
        sit behind the event-sink seam (a method reference, not a call), so
        the startup-event scanner never classifies them; if this ever moves
        onto a directly-called ``self._log`` site on the startup path, add
        the two names to _STARTUP_EVENT_EXCLUSIONS with that reason.  State
        (_crashed/_last_error, visible via ps) is written by the manager
        before this sink is called, so a logging failure here cannot hide
        the fault.
        """

        if event == "thread_exited":
            self._log("error", "daemon", "daemon.lifecycle.thread_exited", **fields)
        elif event == "thread_recovered":
            self._log("info", "daemon", "daemon.lifecycle.thread_recovered", **fields)
        elif event == "compensation_deadline_failed":
            self._log(
                "error", "daemon", "daemon.lifecycle.compensation_deadline_failed",
                **fields,
            )
        else:
            self._log("error", "daemon", "daemon.lifecycle.thread_error", **fields)

    def _log_lifecycle_operation(self, level: str, event: str, **fields: Any) -> None:
        """Best-effort instrumentation for one lifecycle operation's run.

        The calls below sit on the failure paths of ``_run_lifecycle_operation``
        (a refused admission, a deadline that expired, a terminal non-COMPLETED
        state).  A log sink that raised here would replace the operation's own
        error with a logging error -- the same shape
        ``_mirror_startup_event_to_stderr`` guards against, for the same reason:
        this line exists to describe a failure, so losing it must never be worse
        than the failure it describes.
        """

        try:
            self._log(level, "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - must not mask the failure it describes
            pass

    def _lifecycle_status(self) -> JsonObject:
        """The ps-visible health of the lifecycle consumer thread."""

        manager = self._lifecycle_manager
        if manager is None:
            return {"running": False, "crashed": False, "lastError": None}
        return {
            "running": manager.is_running,
            "crashed": manager.crashed,
            "lastError": manager.last_error,
        }

    def _ipc_lifecycle_start(self, method, params) -> Any:
        is_smolvm = method == "lifecycle.start-smolvm"
        if is_smolvm != (params.get("executionRuntime") is not None):
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "executionRuntime requires lifecycle.start-smolvm")
        # The IPC key stays "provider" so new CLIs and old daemons can
        # mix in either direction; locally it names a harness.
        harness = _required_string(params.get("provider"), "provider")
        if harness == "lark":
            raise DaemonRequestError(
                ipc_errors.USE_ADAPTER_COMMAND,
                "Lark is an external-platform adapter; use 'hyprial adapter start <name>'",
            )
        try:
            name = self.agents.native_actor(
                _required_string(params.get("name"), "name")
            )
        except AgentError as error:
            raise DaemonRequestError(error.code, str(error)) from error
        spec = HarnessLaunchSpec.from_json(
            {**params, "provider": harness, "name": name}, "provider"
        )
        if is_smolvm:
            from hyprial.daemon.impl.harnesses.smolvm_runtime import validate_materials
            assert spec.execution_runtime is not None
            validate_materials(spec.execution_runtime)
        hosted_owner = self._host_invited_owner(name)
        if hosted_owner is not None:
            if spec.pinned_owner is None:
                spec = replace(spec, pinned_owner=hosted_owner)
            elif spec.pinned_owner != hosted_owner:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "start cannot re-own a host-invited agent; "
                    f"registry row is owned by {hosted_owner}",
                )
        if (
            spec.pinned_owner is not None
            and spec.pinned_owner != self.owner
            and spec.pinned_owner != hosted_owner
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "start cannot create a foreign-owner entity; use transfer receive",
            )
        # An explicit resume request: only when the CALLER sent a ref.
        # A spec that merely carries one (every running spec does after
        # sync_harness_session_refs) keeps #190's quiet fallback on the
        # daemon-restart path; a person who asked for a conversation must
        # get that conversation or a refusal, never a fresh session.
        resume_ref = spec.session_ref if "sessionRef" in params else None
        if resume_ref is not None:
            self._require_resumable_session(spec, resume_ref)
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        prior_agent = self.agents.get(actor_uri)
        operation_id = str(
            params.get("operationId")
            or f"lifecycle-start:{uuid4().hex}"
        )
        agent_for_restore = self.agents.get(spec.name)
        if agent_for_restore is not None:
            self.agents.clear_restore_disposition(
                spec.name,
                expected_entity_token=agent_for_restore.entity_token,
            )
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=agent_for_restore.entity_token,
                suppressed=False,
            )
        try:
            result = self._run_lifecycle_operation(
                LifecycleOperation.create(
                    operation_id,
                    self._lifecycle_spec(spec),
                )
            )
        except BaseException:
            raise
        if resume_ref is not None:
            # The create operation has settled; readiness is expected to
            # be there already, so the check waits one margin, not a
            # budget of its own.
            self._verify_started_resume(
                spec, resume_ref, timeout=LIFECYCLE_WAIT_MARGIN_SECONDS
            )
        handover = (
            HandoverNotice(
                actor=prior_agent.actor,
                previous_harness=prior_agent.last_harness,
                previous_session_id=prior_agent.last_session_id,
                next_harness=harness,
            )
            if prior_agent is not None
            and prior_agent.last_harness is not None
            and prior_agent.last_harness != harness
            else None
        )
        return {
            "ok": True,
            "id": f"{harness}:{name}",
            "operationId": operation_id,
            "changed": bool(result.completed_effects),
            "actor": actor_uri,
            **({"sessionRef": resume_ref} if resume_ref is not None else {}),
            **(
                {"harnessHandover": handover.to_json()}
                if handover is not None
                else {}
            ),
        }

    def _ipc_down(self, params) -> Any:
        state = self.desired_state.load()
        removed: list[str] = []
        if params.get("all") is True:
            for spec in state.harnesses:
                if spec.harness == "lark" and self._lark_client is not None:
                    self._lark_client.stop(spec.name)
                else:
                    operation_id = f"lifecycle-down:{uuid4().hex}"
                    self._run_lifecycle_operation(
                        LifecycleOperation.deactivate(
                            operation_id,
                            self._lifecycle_spec(spec),
                        )
                    )
                if spec.containerized:
                    self._retire_container_artifacts(
                        spec.harness, spec.name
                    )
                removed.append(f"{spec.harness}:{spec.name}")
        else:
            target = _required_string(params.get("target"), "target")
            harness = params.get("provider")
            for spec in state.harnesses:
                if (
                    harness is None
                    and target in {spec.name, f"{spec.harness}:{spec.name}"}
                ) or (harness == spec.harness and target == spec.name):
                    if spec.harness == "lark" and self._lark_client is not None:
                        self._lark_client.stop(spec.name)
                    else:
                        operation_id = f"lifecycle-down:{uuid4().hex}"
                        self._run_lifecycle_operation(
                            LifecycleOperation.deactivate(
                                operation_id,
                                self._lifecycle_spec(spec),
                            )
                        )
                    if spec.containerized:
                        self._retire_container_artifacts(
                            spec.harness, spec.name
                        )
                    removed.append(f"{spec.harness}:{spec.name}")
            if not removed:
                # Nothing in desired state matched, but `down` is also the
                # documented remedy when a start is refused. Release the
                # binding anyway so a connector whose spec vanished
                # without a clean stop can never strand its agent's name
                # behind an error that tells the user to run this.
                released = self._agent_name_for(target)
                if released is not None:
                    self._release_agent_binding(self.agents.uri_for(released))
        return {"ok": True, "removed": removed}
