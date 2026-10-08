"""Runtime graph composition, service phases: lifecycle/PAC/workflow/routine adoption and startup finalization."""

from __future__ import annotations

from __future__ import annotations
import time
from typing import TYPE_CHECKING
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.dispatch.alarm import DispatchAlarm
from hyprial.daemon.impl.application.ports import RoutineRuntimeDeps
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.wiring.graph import (
    _StartupGraph,
)


class _WiringServicesMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _startup_adopt_domain_services(self, graph: _StartupGraph) -> None:
        """Start lifecycle/PAC/workflow/routine services with degradation paths."""
        from hyprial.daemon.impl.pac.actors.daemon  import (
            DaemonActorRuntime,
            DaemonPacNotificationSender,
            PacActorService,
        )
        from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
        from hyprial.daemon.impl.pac.storage.store import default_database_path
        from hyprial.daemon.impl.pac.actors.gc  import PacGc

        # U0BRACE: nothing else may live here.  This used to be the call site
        # of ``_bootstrap_lifecycle_harnesses``, which submitted one create
        # saga per non-Lark running row -- the same selection predicate
        # ``_restore_harnesses`` uses, so every such row had TWO owners and
        # only timing decided which one started the process (the loser
        # either deferred or, worse, reset the winner's record and started
        # a second process).  Restore owns those rows now;
        # ``tests/test_daemon_restart_ownership.py`` pins that the startup
        # window submits no lifecycle operations at all.
        from hyprial.daemon.impl.pac.storage.legacy import cutover

        workflow_cutover_ok = False
        try:
            self._legacy_workflow_cutover = cutover(self.state_dir)
        except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
            self._legacy_workflow_cutover = None
            self._log(
                "error",
                "workflow",
                "workflow.cutover_failed",
                phase="cutover",
                exceptionClass=type(error).__name__,
                detail=str(error),
                databasePath=str(self.state_dir / "workflows.sqlite3"),
                sealed=False,
                disabledCapabilities=(
                    "workflow.dispatch",
                    "workflow.mutation",
                    "workflow.recovery",
                    "workflow.remote",
                    "routine.dispatch",
                    "pac.actor.cadence",
                    "pac.gc",
                ),
            )
        else:
            workflow_cutover_ok = True
            self._log(
                "info",
                "workflow",
                "workflow.cutover_applied",
                present=self._legacy_workflow_cutover["present"],
                at=self._legacy_workflow_cutover["at"],
                sealed=self._legacy_workflow_cutover["sealed"],
                terminated=self._legacy_workflow_cutover["terminated"],
            )
            for cancelled in self._legacy_workflow_cutover["cancelled"]:
                self._log(
                    "warn",
                    "workflow",
                    "workflow.cutover_cancelled",
                    runId=cancelled["runId"],
                    sender=cancelled["sender"],
                    table=cancelled["table"],
                    priorState=cancelled["priorState"],
                    reason=cancelled["reason"],
                    at=self._legacy_workflow_cutover["at"],
                )

        workflow_disabled = (
            "workflow.dispatch",
            "workflow.mutation",
            "workflow.recovery",
            "workflow.remote",
            "routine.dispatch",
            "pac.actor.cadence",
            "pac.gc",
        )

        def suspend_pac_actor() -> None:
            actor = self._pac_actor_service
            self._pac_actor_service = None
            if actor is None:
                return
            try:
                if actor.close(5.0) is False:
                    raise RuntimeError("PAC actor service did not drain")
            except Exception as error:  # noqa: BLE001 - preserve startup
                self._log(
                    "error",
                    "pac",
                    "workflow.degrade_cleanup_failed",
                    phase="pac-actor",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                )
                self._degraded_workflow_handles.append(actor)

        def suspend_pac_gc() -> bool:
            collector = self._pac_gc
            self._pac_gc = None
            if collector is None:
                return True
            try:
                if collector.close(5.0) is False:
                    raise RuntimeError("PAC GC service did not stop")
            except Exception as error:  # noqa: BLE001 - preserve startup
                self._log(
                    "error",
                    "pac",
                    "workflow.degrade_cleanup_failed",
                    phase="pac-gc",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                )
                self._degraded_workflow_handles.append(collector)
                return False
            return True

        def degrade_workflow_components() -> None:
            if not suspend_pac_gc():
                # Disable public workflow admission, but do not close objects
                # an undrained destructive collector can still be using.
                for attribute in (
                    "_pac_actor_service", "_pac_graph_authority",
                    "_remote_workflow", "_workflow_service",
                ):
                    handle = getattr(self, attribute)
                    setattr(self, attribute, None)
                    if handle is not None:
                        self._degraded_workflow_handles.append(handle)
                return
            suspend_pac_actor()
            graph_authority = self._pac_graph_authority
            self._pac_graph_authority = None
            if graph_authority is not None:
                try:
                    if not graph_authority.close(5.0):
                        raise RuntimeError("PAC graph authority did not drain")
                except Exception as error:
                    self._log(
                        "error", "pac", "workflow.degrade_cleanup_failed",
                        phase="pac-graph", exceptionClass=type(error).__name__,
                        detail=str(error),
                    )
                    self._degraded_workflow_handles.append(graph_authority)
            remote = self._remote_workflow
            self._remote_workflow = None
            if remote is not None:
                try:
                    remote.close()
                except Exception as error:  # noqa: BLE001 - preserve startup
                    self._log(
                        "error",
                        "pac",
                        "workflow.degrade_cleanup_failed",
                        phase="remote",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                    )
                    self._degraded_workflow_handles.append(remote)
            workflow_service = self._workflow_service
            self._workflow_service = None
            if workflow_service is not None:
                try:
                    if workflow_service.close() is False:
                        raise RuntimeError("workflow service did not drain")
                except Exception as error:  # noqa: BLE001 - preserve startup
                    self._log(
                        "error",
                        "pac",
                        "workflow.degrade_cleanup_failed",
                        phase="workflow",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                    )
                    self._degraded_workflow_handles.append(workflow_service)

        from hyprial.daemon.impl.dispatch.remote import RemoteWorkflow
        dispatch_alarm = DispatchAlarm(graph.inbox.alarm_emitter, self._workflow_deliver_user, self._logger)
        pac_graph_ready = False
        if workflow_cutover_ok:
            try:
                self._pac_graph_authority = PacGraphAuthority(
                    default_database_path(self.state_dir),
                    DaemonPacNotificationSender(self),
                    logger=self._log,
                )
            except Exception as error:
                self._log(
                    "error", "pac", "pac.graph_authority_unavailable",
                    errorType=type(error).__name__, detail=str(error)[:500],
                    disabledCapabilities=workflow_disabled,
                )
            else:
                pac_graph_ready = True
        if pac_graph_ready:
            try:
                self._pac_actor_service = PacActorService(
                    state_dir=self.state_dir,
                    reference_root=self.hyprial_home,
                    runtime=DaemonActorRuntime(self),
                    sender=DaemonPacNotificationSender(self),
                    daemon_epoch=self.epoch,
                    logger=self._log,
                    graph_authority=self._pac_graph_authority,
                )
            except Exception as error:  # noqa: BLE001 - cadence is optional
                self._pac_actor_service = None
                self._log(
                    "error",
                    "pac",
                    "workflow.pac_actor_unavailable",
                    phase="pac-actor",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=workflow_disabled,
                )
            try:
                self._pac_gc = PacGc(
                    state_dir=self.state_dir,
                    application=self,
                    runtime=DaemonActorRuntime(self),
                    logger=self._log,
                )
            except Exception as error:  # noqa: BLE001 - collection is optional
                self._pac_gc = None
                self._log(
                    "error",
                    "pac",
                    "workflow.pac_gc_unavailable",
                    phase="pac-gc",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=("pac.gc",),
                )
        if pac_graph_ready:
            try:
                self._workflow_service = GraphWorkflowService(
                    state_dir=self.state_dir, owner=self.owner, machine=self.node_id,
                    sender=DaemonPacNotificationSender(self),
                    admit=self._workflow_admit, logger=self._log,
                    delivery_receipt=self._pac_graph_authority,
                    graph_authority=self._pac_graph_authority,
                )
            except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
                self._log(
                    "error",
                    "pac",
                    "workflow.recovery_unavailable",
                    phase="construct",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=workflow_disabled,
                )
                suspend_pac_gc()
                suspend_pac_actor()
            else:
                try:
                    self._remote_workflow = RemoteWorkflow(self, graph.transport)
                except Exception as error:  # noqa: BLE001 - remote is optional
                    self._remote_workflow = None
                    self._log(
                        "error",
                        "pac",
                        "workflow.remote_unavailable",
                        phase="remote-construction",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                        disabledCapabilities=(
                            "workflow.remote.admission",
                            "workflow.remote.completion",
                            "workflow.remote.returns",
                            "routine.remote.dispatch",
                        ),
                    )
                try:
                    adopted = self._workflow_service.recover()
                except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
                    self._log(
                        "error",
                        "pac",
                        "workflow.recovery_unavailable",
                        phase="recover",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                        disabledCapabilities=workflow_disabled,
                    )
                    degrade_workflow_components()
                else:
                    if adopted:
                        self._log("info", "daemon", "workflow.recovered", runs=adopted)

        # Self-drive routines (design-selfdrive-routine): deterministic duty
        # cycles producing one PAC graph per task (U3).  The old dispatcher is
        # no longer in this path; the alarm sink still comes from it, which is
        # U2 residue the retirement removes in U6/U7.
        adopted_routines = 0
        if self._workflow_service is not None:
            try:
                # The routine runtime (service + PAC dispatch + coordinator)
                # is a biz capability injected through the required factory
                # port; the daemon supplies only its own dependencies.
                routine_runtime = self._routine_runtime_factory(
                    RoutineRuntimeDeps(
                        state_dir=self.state_dir,
                        hyprial_home=self.hyprial_home,
                        owner=self.owner,
                        logger=self._logger,
                        alarm=dispatch_alarm,
                        deliver_task=self._deliver_routine_task,
                        clock_ms=lambda: time.time_ns() // 1_000_000,
                        resolve_principal=self._resolve_routine_principal,
                        # Stored bare-name addresses migrate ONLY via this
                        # machine's agents registry (approved plan Q1).
                        migrate_address=self._migrate_stored_routine_address,
                        workflow_service=self._workflow_service,
                        graph_authority=self._pac_graph_authority,
                        ensure_coordinator=lambda routine: self._ensure_routine_coordinator(
                            routine, recovering=False
                        ),
                        retire_coordinator=self._retire_routine_coordinator,
                        close_graph=lambda graph_id, actor: (
                            self._pac_graph_authority.close_graph(graph_id, actor=actor)
                            if self._pac_graph_authority is not None
                            else None
                        ),
                        compensate_agent=self._compensate_default_agent_incarnation,
                    )
                )
                assert routine_runtime is not None, (
                    "routine_runtime_factory must return a runtime or raise; "
                    "None is reserved for degraded workflow-less startup"
                )
                self._routine_service = routine_runtime.service
                self._routine_coordinator = routine_runtime.coordinator
                self._routine_exists_probe = routine_runtime.routine_exists
                adopted_routines = self._routine_service.recover()
                self._reconcile_routine_coordinators()
            except Exception as error:  # noqa: BLE001 - routine is optional
                routine = self._routine_service
                self._routine_service = None
                if routine is not None:
                    try:
                        routine.close()
                    except Exception as cleanup_error:  # noqa: BLE001
                        self._log(
                            "error",
                            "pac",
                            "workflow.degrade_cleanup_failed",
                            phase="routine",
                            exceptionClass=type(cleanup_error).__name__,
                            detail=str(cleanup_error),
                        )
                self._log(
                    "error",
                    "pac",
                    "routine.recovery_unavailable",
                    phase="routine",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=("routine.dispatch",),
                )
        if adopted_routines:
            self._log("info", "daemon", "routine.recovered", routines=adopted_routines)
        # U3 deleted the retired dispatcher's rows when the store opened.  The
        # ruling was 「迁移时直接删除」, and a deletion whose only trace is an
        # absence cannot be checked afterwards -- so name the rows here, once,
        # at the startup that dropped them.
        dropped = (
            self._routine_service.migrated_u3
            if self._routine_service is not None
            else {"effects": (), "inFlight": ()}
        )
        if dropped["effects"] or dropped["inFlight"]:
            self._log(
                "info",
                "routine",
                "routine.migrated_u3",
                effects=list(dropped["effects"]),
                inFlight=list(dropped["inFlight"]),
            )

    def _startup_finalize_runtime(self, graph: _StartupGraph) -> None:
        """Restore persona routes, seed bindings, report recovery completion."""
        self._restore_persona_routes()
        try:
            try:
                graph.org_endpoint.publish_accepted()
            except Exception as error:  # noqa: BLE001 - startup stays available
                self._log(
                    "warn",
                    "org",
                    "org.context.publish_failed",
                    detail=str(error),
                )
            self._require_org_context_bridge().publish_accepted()
            self._clean_dead_interactive_sessions()
            # Startup restoration is part of the same Session/route domain as
            # live heartbeat and expiry.  Create its owner before replay so
            # production never falls back to the legacy application lock.
            self._ensure_session_route_coordinator()
            self._restore_interactive_routes()
            # Bindings are per daemon generation; re-derive them from desired
            # state so the A1 uniqueness gate survives a restart.
            self._seed_agent_bindings()
            # The registry imported any pre-sqlite agents/*.json records at
            # construction; the registry itself has no logging seam, so the
            # import is reported here where it can be seen.
            if self.agents.imported_legacy:
                self._log(
                    "info",
                    "agents",
                    "agent.registry.imported",
                    files=list(self.agents.imported_legacy),
                    detail=(
                        "pre-sqlite agent records imported into "
                        "agents.sqlite3; originals kept as *.json.imported"
                    ),
                )
            # Adapter pins moved into the agents database; drain any legacy
            # desired-state entries before adapters (their workers query the
            # daemon's pin index) come up.
            self._migrate_legacy_channel_pins()
        except BaseException:
            self._close()
            raise
        self._log(
            "info",
            "daemon",
            "service.recovery.completed",
            # attempted/restored/failed are deliberately absent: the event
            # bridge stopped restoring harnesses, so its summary reports 0 for
            # all three regardless of what was declared. Logging them here
            # described a recovery that had not happened, in a success shape
            # -- failed=0 reads as "nothing went wrong" when the truth was
            # "nothing ran". The real counts are in harness.recovery.completed
            # and adapter.recovery.completed, emitted where the work occurs.
            previousRunUnclean=graph.recovery.previous_run_unclean,
        )
