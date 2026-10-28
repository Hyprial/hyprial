"""Lifecycle manager start/step execution and the lifecycle.start/down IPC families."""

from __future__ import annotations

from __future__ import annotations
import time
from dataclasses import replace
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.daemon.impl.adapters.spec import is_adapter_provider
from hyprial.identity import (
    RUNTIME_INTERACTIVE,
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
from hyprial.kernel import DECLARED_HARNESSES, HarnessLaunchSpec
from hyprial.daemon.impl.composition  import (
    AgentIdentityProjectionView,
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
    merge_harness_launch_args,
)
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleKind,
    LifecycleOperation,
    LifecyclePorts,
    LifecycleSpec,
    LifecycleState,
)
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator  import (
    LifecycleCoordinator,
    LifecycleReservationOutcomeUnknown,
)
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
            agent_identity=AgentIdentityProjectionView(self._agent_session_domains),
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
        self._start_transfer_source(harnesses, manager)

    def _lifecycle_spec(self, spec: HarnessLaunchSpec) -> LifecycleSpec:
        if spec.harness not in DECLARED_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "agent lifecycle requires a declared worker harness; "
                "use adapter commands for external-platform adapters",
            )
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
        try:
            admission = manager.submit(operation)
        except LifecycleReservationOutcomeUnknown as error:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.reservation.outcome_unknown",
                operationId=operation_id,
                kind=kind,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                f"lifecycle reservation outcome is unknown: {operation_id}",
                {"operationId": error.operation_id},
            ) from error
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
            if launch.harness in DECLARED_HARNESSES:
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

    def _start_failure_with_harness_error(
        self,
        spec: HarnessLaunchSpec,
        error: DaemonRequestError,
        *,
        last_error: str | None = None,
    ) -> DaemonRequestError:
        """Attach the worker's own launch refusal to a start failure (D3).

        A start can fail before its real cause reaches the caller: the pi SDK
        bridge refuses a headless launch without ``--approve`` on a background
        connect thread, so ``lifecycle.start`` answers with a generic resume or
        deadline code while the concrete refusal sits on the worker's
        ``last_error``.  Carry that text as additive ``data.lastError`` -- the
        outer code and message are untouched -- so the operator sees the real,
        actionable cause.  The text is redacted and bounded; only the static
        reason the launcher/process published travels.
        """
        data = dict(error.data) if isinstance(error.data, dict) else {}
        if "lastError" in data:
            return error
        detail = last_error if last_error is not None else self._harness_last_error(spec)
        if detail is None:
            return error
        data["lastError"] = detail
        return DaemonRequestError(error.code, str(error), data)

    def _harness_last_error(self, spec: HarnessLaunchSpec) -> str | None:
        harnesses = self._harnesses
        if harnesses is None:
            return None
        try:
            rows = harnesses.status()
        except Exception:  # noqa: BLE001 - diagnostics must not mask the start failure
            return None
        for row in rows:
            detail = row.get("error") if isinstance(row, dict) else None
            if (
                isinstance(row, dict)
                and row.get("runtime") == spec.harness
                and row.get("name") == spec.name
                and isinstance(detail, str)
                and detail
            ):
                from hyprial.kernel import redact

                return str(redact(detail))[:500]
        return None

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

    def _compensate_replace(
        self, actor_uri: str, spec: HarnessLaunchSpec
    ) -> None:
        """Best effort: remove a failed switch's headless intent/binding."""

        try:
            self.desired_state.remove_harness(spec.harness, spec.name)
        except Exception as error:  # noqa: BLE001 - preserve the start failure
            self._log(
                "warn",
                "agents",
                "agent.switch.compensation_failed",
                actor=actor_uri,
                harness=spec.harness,
                name=spec.name,
                detail=str(error)[:300],
            )
        self._agent_liveness.release(actor_uri)

    def _ipc_lifecycle_start(self, method, params) -> Any:
        resume_preflight = method == "lifecycle.resume.preflight"
        is_smolvm = method == "lifecycle.start-smolvm"
        if not resume_preflight and is_smolvm != (
            params.get("executionRuntime") is not None
        ):
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "executionRuntime requires lifecycle.start-smolvm")
        # The IPC key stays "provider" so new CLIs and old daemons can
        # mix in either direction; locally it names a harness.
        harness = _required_string(params.get("provider"), "provider")
        if is_adapter_provider(harness):
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
        from hyprial.daemon.impl.bootstrap.names import require_agent_name_available

        require_agent_name_available(self, name)
        spec = HarnessLaunchSpec.from_json(
            {**params, "provider": harness, "name": name}, "provider"
        )
        # The agent record is durable launch intent: an explicit request is
        # merged with (and wins over) the harnessArgs stored by an earlier
        # ``agent create``/registration, so a resident pi agent that was once
        # started with ``-- --approve`` starts the same way again.  This is the
        # single funnel for ``hyprial start``, daemon restore re-submission and
        # the routine coordinator's wake, which is why the merge lives here.
        registered_agent = self.agents.get(name)
        if registered_agent is not None:
            spec = replace(
                spec,
                args=merge_harness_launch_args(
                    registered_agent.args_for(harness), spec.args, harness=harness
                ),
            )
        if resume_preflight:
            resume_ref = _required_string(params.get("sessionRef"), "sessionRef")
            self._require_resumable_session(spec, resume_ref)
            existing_agent = self.agents.get(name)
            if existing_agent is None:
                raise DaemonRequestError(
                    ipc_errors.AGENT_NOT_FOUND,
                    f"cannot resume {harness} session {resume_ref!r}: "
                    f"no existing agent named {name!r}",
                )
            return {
                "ok": True,
                "resumable": True,
                "harness": harness,
                "name": name,
                "sessionRef": resume_ref,
                "agentSnapshot": existing_agent.to_json(),
            }
        if "sessionRef" not in params:
            # A managed restart reuses the desired spec but lifecycle.start
            # rebuilds it from the IPC payload.  Keep a session ref learned
            # from the running connector; only the caller can explicitly
            # replace it (card 708).
            prior = next(
                (
                    item
                    for item in self.desired_state.load().harnesses
                    if item.harness == spec.harness and item.name == spec.name
                ),
                None,
            )
            if prior is not None and prior.session_ref is not None:
                spec = replace(spec, session_ref=prior.session_ref)
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
        if params.get("replaceInteractive") is True and resume_ref is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "replaceInteractive requires sessionRef; a switch must preserve "
                "the conversation",
            )
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
            # This IPC caller may wait on the short journal lane; neither the
            # harness mailbox nor eligibility publication may read SQLite.
            # This observation does not replace the harness owner's start hold.
            custody = self._hydrate_transfer_source(
                spec.name, agent_for_restore.entity_token,
            )
            if custody is None or custody.blocks_start:
                raise DaemonRequestError(
                    ipc_errors.TRANSFER_STARTS_HELD
                    if custody is not None and custody.state == "held"
                    else ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                    "source custody prevents explicit start; an authenticated override is required",
                )
            self.agents.clear_restore_disposition(
                spec.name,
                expected_entity_token=agent_for_restore.entity_token,
            )
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=agent_for_restore.entity_token,
                suppressed=False,
            )
        replaced_interactive = False
        if params.get("replaceInteractive") is True:
            incumbent = self._agent_liveness.live_binding(actor_uri)
            if incumbent is not None and incumbent.runtime == RUNTIME_INTERACTIVE:
                # The caller explicitly asked for this actor's interactive
                # connector to step aside.  The daemon proves the carrier's
                # PID/birth fence, signals it, and waits for exit before the
                # route is unregistered; the same sessionRef then follows into
                # the headless start below.
                self._stop_agent_runtime(
                    actor_uri, keep=None, terminate_interactive=True
                )
                self._agent_liveness.release(actor_uri)
                replaced_interactive = True
                self._log(
                    "info",
                    "agents",
                    "agent.binding.replaced",
                    actor=actor_uri,
                    previousRuntime=incumbent.runtime,
                    previousSessionId=incumbent.session_id,
                    harness=harness,
                )
        try:
            result = self._run_lifecycle_operation(
                LifecycleOperation.create(
                    operation_id,
                    self._lifecycle_spec(spec),
                )
            )
        except DaemonRequestError as start_error:
            if replaced_interactive:
                self._compensate_replace(actor_uri, spec)
            raise self._start_failure_with_harness_error(spec, start_error) from start_error
        established_ref = resume_ref
        if resume_ref is not None:
            # The create operation has settled; readiness is expected to
            # be there already, so the check waits one margin, not a
            # budget of its own.  Capture the worker's own published refusal
            # BEFORE the strict check: on failure it deactivates the worker,
            # which removes the row the refusal lives on.
            last_error = self._harness_last_error(spec)
            try:
                established_ref = self._verify_started_resume(
                    spec, resume_ref, timeout=LIFECYCLE_WAIT_MARGIN_SECONDS
                )
            except DaemonRequestError as resume_error:
                if replaced_interactive:
                    self._compensate_replace(actor_uri, spec)
                raise self._start_failure_with_harness_error(
                    spec, resume_error, last_error=last_error
                ) from resume_error
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
        warnings = [
            warning
            for row in (self._harnesses.status() if self._harnesses is not None else ())
            if row.get("runtime") == spec.harness and row.get("name") == spec.name
            for warning in row.get("warnings", ())
        ]
        return {
            "ok": True,
            **({"warnings": warnings} if warnings else {}),
            "id": f"{harness}:{name}",
            "operationId": operation_id,
            "changed": bool(result.completed_effects),
            "actor": actor_uri,
            **(
                {"sessionRef": established_ref}
                if resume_ref is not None
                else {}
            ),
            **(
                {"harnessHandover": handover.to_json()}
                if handover is not None
                else {}
            ),
        }

    def _ipc_down(self, params) -> Any:
        state = self.desired_state.load()
        agents_only = params.get("agentsOnly") is True
        removed: list[str] = []
        if params.get("all") is True:
            if not agents_only and self._lark_client is not None:
                for adapter in state.adapters:
                    self._lark_client.stop(adapter.name)
                    removed.append(f"{adapter.provider}:{adapter.name}")
            for spec in state.harnesses:
                operation_id = f"lifecycle-down:{uuid4().hex}"
                self._run_lifecycle_operation(
                    LifecycleOperation.deactivate(operation_id, self._lifecycle_spec(spec))
                )
                if spec.containerized:
                    self._retire_container_artifacts(
                        spec.harness, spec.name
                    )
                removed.append(f"{spec.harness}:{spec.name}")
        else:
            target = _required_string(params.get("target"), "target")
            harness = params.get("provider")
            matched_adapter = False
            if not agents_only:
                for adapter in state.adapters:
                    if ((harness is None and target in {adapter.name, f"{adapter.provider}:{adapter.name}"})
                            or (harness == adapter.provider and target == adapter.name)):
                        matched_adapter = True
                        if self._lark_client is not None:
                            self._lark_client.stop(adapter.name)
                            removed.append(f"{adapter.provider}:{adapter.name}")
            for spec in state.harnesses:
                if (
                    harness is None
                    and target in {spec.name, f"{spec.harness}:{spec.name}"}
                ) or (harness == spec.harness and target == spec.name):
                    operation_id = f"lifecycle-down:{uuid4().hex}"
                    self._run_lifecycle_operation(
                        LifecycleOperation.deactivate(operation_id, self._lifecycle_spec(spec))
                    )
                    if spec.containerized:
                        self._retire_container_artifacts(
                            spec.harness, spec.name
                        )
                    removed.append(f"{spec.harness}:{spec.name}")
            if not removed and not matched_adapter and not is_adapter_provider(harness):
                # Nothing in desired state matched, but `down` is also the
                # documented remedy when a start is refused. Release the
                # binding anyway so a connector whose spec vanished
                # without a clean stop can never strand its agent's name
                # behind an error that tells the user to run this.
                released = self._agent_name_for(target)
                if released is not None:
                    self._release_agent_binding(self.agents.uri_for(released))
        return {"ok": True, "removed": removed}
