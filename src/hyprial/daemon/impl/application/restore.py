"""Startup restore: adapter/harness restoration, eligibility projection, dormant wake scans and the restore gate thread."""

from __future__ import annotations

from __future__ import annotations
import threading
from dataclasses import replace
import time
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.pac.contracts.restore import PacRestoreFacts
from hyprial.daemon.impl.correlation.bounded_cadence  import BoundedCadence
from hyprial.identity import (
    Agent,
)
from hyprial.identity import (
    RestorePolicyError,
    desired_generation,
    is_inactive,
)
from hyprial.kernel import DaemonStartupPhase
from hyprial.kernel import PortAdmission
from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import merge_harness_launch_args
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    RestoreEligibilityProjection,
)
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
    LifecycleState,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.shutdown import (
    _RESTORE_THREAD_JOIN_TIMEOUT,
)
from hyprial.daemon.impl.application.startup import (
    _mirror_startup_event_to_stderr,
)


class _RestoreMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _restore_adapters(self) -> None:
        assert self._lark_client is not None
        state = self.desired_state.load()
        names = tuple(
            sorted(spec.name for spec in state.adapters)
        )
        attempted = 0
        restored = 0
        failed = 0
        for name in names:
            # Restore now runs on its own thread, so it must answer a stop
            # itself: a shutdown that lands mid-restore cannot wait out a
            # full fleet's worth of adapter starts.
            if self.stop_event.is_set():
                break
            attempted += 1
            try:
                self._lark_client.start(name)
                restored += 1
            except (DomainCommandError, RuntimeError):
                failed += 1
        self._log(
            "info",
            "adapter",
            "adapter.recovery.completed",
            attempted=attempted,
            restored=restored,
            failed=failed,
        )

    def _pending_restore_work(self, agent: Agent) -> bool:
        inbox = self._inbox
        if inbox is None:
            return False
        reader = getattr(inbox, "has_pending_work", None)
        if callable(reader):
            return bool(reader(agent.uri) or reader(agent.actor))
        pending = getattr(inbox, "pending_messages", None)
        return bool(
            callable(pending)
            and (pending(agent.uri) or pending(agent.actor))
        )

    def _pac_restore_fact(self, actor: str) -> PacRestoreFacts | None:
        authority = self._pac_graph_authority
        reader = getattr(authority, "restore_facts", None)
        if not callable(reader):
            return None
        try:
            return reader(actor)
        except Exception as error:  # noqa: BLE001 - unavailable facts fail open
            self._log(
                "warn",
                "daemon",
                "restore-pac-facts-degraded",
                actor=actor,
                errorType=type(error).__name__,
            )
            return None

    def _publish_restore_eligibility(
        self,
        *,
        spec: HarnessLaunchSpec,
        entity_token: str,
        suppressed: bool,
    ) -> None:
        harnesses = self._harnesses
        if harnesses is None:
            return
        submit = getattr(harnesses, "submit_restore_eligibility", None)
        if not callable(submit):
            return
        current_agent = self.agents.projection(spec.name)
        if current_agent is None or current_agent.entity_token != entity_token:
            return
        current_spec = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.harness == spec.harness and item.name == spec.name
            ),
            None,
        )
        if (
            current_spec is None
            or desired_generation(current_spec) != desired_generation(spec)
        ):
            return
        with self._restore_eligibility_lock:
            self._restore_eligibility_version += 1
            version = self._restore_eligibility_version
        # Activity/degraded policy cannot clear a migration custody guard.
        if getattr(self, "_transfer_p2", None) is not None:
            custody = self._transfer_source_state(spec.name, entity_token)
            suppressed = suppressed or custody.blocks_start
        eligibility = RestoreEligibilityProjection(
            actor=spec.name,
            entity_token=entity_token,
            desired_generation=desired_generation(spec),
            suppressed=suppressed,
            source_generation=self._agent_session_domains.agent.generation,
            source_version=version,
        )
        if self._restore_classifying:
            with self._restore_eligibility_lock:
                if (
                    spec.name in self._pending_restore_eligibility
                    or len(self._pending_restore_eligibility)
                    < self._restore_eligibility_capacity
                ):
                    self._pending_restore_eligibility[spec.name] = eligibility
            return
        admission = submit(eligibility)
        with self._restore_eligibility_lock:
            if admission is PortAdmission.ACCEPTED:
                self._pending_restore_eligibility.pop(spec.name, None)
            elif (
                spec.name in self._pending_restore_eligibility
                or len(self._pending_restore_eligibility)
                < self._restore_eligibility_capacity
            ):
                self._pending_restore_eligibility[spec.name] = eligibility
        if admission is PortAdmission.OVERLOADED:
            self._log(
                "warn",
                "daemon",
                "harness.restore_eligibility_overloaded",
                actor=spec.name,
            )

    def _retry_restore_eligibility(self) -> None:
        harnesses = self._harnesses
        if harnesses is None:
            return
        submit = getattr(harnesses, "submit_restore_eligibility", None)
        if not callable(submit):
            return
        with self._restore_eligibility_lock:
            pending = tuple(self._pending_restore_eligibility.items())[
                : self._restore_eligibility_batch
            ]
        for actor, eligibility in pending:
            if not self._restore_eligibility_current(eligibility):
                with self._restore_eligibility_lock:
                    if self._pending_restore_eligibility.get(actor) == eligibility:
                        self._pending_restore_eligibility.pop(actor, None)
                continue
            admission = submit(eligibility)
            if admission is PortAdmission.ACCEPTED:
                with self._restore_eligibility_lock:
                    if self._pending_restore_eligibility.get(actor) == eligibility:
                        self._pending_restore_eligibility.pop(actor, None)
            elif admission is PortAdmission.OVERLOADED:
                break

    def _restore_eligibility_current(
        self, eligibility: RestoreEligibilityProjection
    ) -> bool:
        agent = self.agents.projection(eligibility.actor)
        if agent is None or agent.entity_token != eligibility.entity_token:
            return False
        spec = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.name == eligibility.actor
            ),
            None,
        )
        return bool(
            spec is not None
            and desired_generation(spec) == eligibility.desired_generation
        )

    def _restore_policy_or_degraded(self):
        """Retain the dev refresh entry through the policy storage owner."""
        try:
            self._restore_policy.refresh(wait=True)
            projection = self._restore_policy.projection()
            if projection.degraded is not None:
                raise RestorePolicyError(projection.degraded)
        except (OSError, RestorePolicyError, TimeoutError) as error:
            self._restore_policy_degraded = str(error)
            self._log("warn", "daemon", "restore-policy-degraded",
                      errorType=type(error).__name__, detail=str(error)[:500])
            return None
        self._restore_policy_degraded = None
        return projection.policy

    def _restore_threshold_for_status(self) -> int:
        return self._restore_policy.projection().policy.threshold_ms

    def _is_restore_suppressed_spec(self, spec: HarnessLaunchSpec) -> bool:
        """Legacy query entry, with incarnation-fenced owner mutations."""
        agent = self.agents.get(spec.name)
        projection = self.agents.projection(spec.name)
        if agent is None or projection is None or projection.restore_disposition is None:
            return False
        disposition = projection.restore_disposition
        policy = self._restore_policy_or_degraded()
        try:
            allowed = (
                policy is None
                or disposition.desired_generation != desired_generation(spec)
                or self._pending_restore_work(agent)
                or policy.policy_for(agent.actor) == "always"
                or projection.block is not None
            )
        except Exception as error:  # noqa: BLE001 - unreadable override restores
            self._restore_policy_degraded = str(error)
            allowed = True
        if allowed:
            self.agents.clear_restore_disposition(
                agent.actor, expected_entity_token=agent.entity_token,
                expected_desired_generation=disposition.desired_generation,
                expected_disposition_token=disposition.disposition_token,
            )
            return False
        return True

    def _classify_restore_specs(
        self, specs: tuple[HarnessLaunchSpec, ...]
    ) -> tuple[HarnessLaunchSpec, ...]:
        specs = self._merge_stored_harness_args(specs)
        if getattr(self, "_transfer_p2", None) is not None:
            specs = self._filter_transfer_restore_specs(specs)
        return self._classify_activity_restore_specs(specs)

    def _merge_stored_harness_args(
        self, specs: tuple[HarnessLaunchSpec, ...]
    ) -> tuple[HarnessLaunchSpec, ...]:
        """Re-apply each agent's stored harnessArgs to the desired-state row.

        Desired state is the row that was written by an earlier start; the
        agent record is the same intent's durable home.  A ``hyprial start``
        whose explicit args were merged at admission already wrote the merged
        value here, but a row persisted before that merge (or edited out of
        band) must still restore with the stored flags -- otherwise a resident
        pi agent loses its ``--approve`` on the next daemon restart.
        """

        merged: list[HarnessLaunchSpec] = []
        for spec in specs:
            agent = self.agents.get(spec.name)
            args = (
                spec.args
                if agent is None
                else merge_harness_launch_args(
                    agent.args_for(spec.harness), spec.args, harness=spec.harness
                )
            )
            merged.append(spec if args == spec.args else replace(spec, args=args))
        return tuple(merged)

    def _classify_activity_restore_specs(self, specs):
        self._restore_policy_or_degraded()
        policy_projection = self._restore_policy.projection()
        if policy_projection.degraded is not None or self._restore_policy_degraded is not None:
            if self._restore_policy_degraded_version != policy_projection.version:
                self._restore_policy_degraded_version = policy_projection.version
                self._log(
                    "warn",
                    "daemon",
                    "restore-policy-degraded",
                    detail=policy_projection.degraded or self._restore_policy_degraded,
                )
            for spec in specs:
                agent = self.agents.get(spec.name)
                if agent is None:
                    continue
                self.agents.clear_restore_disposition(
                    agent.actor,
                    expected_entity_token=agent.entity_token,
                )
                self._publish_restore_eligibility(
                    spec=spec,
                    entity_token=agent.entity_token,
                    suppressed=False,
                )
            return specs
        policy = policy_projection.policy
        restored: list[HarnessLaunchSpec] = []
        self._restore_activity_unknown.clear()
        now_ms = self._restore_now_ms()
        for spec in specs:
            agent = self.agents.get(spec.name)
            projection = self.agents.projection(spec.name)
            if agent is None or projection is None:
                restored.append(spec)
                continue
            pac = self._pac_restore_fact(agent.actor)
            if pac is None:
                restored.append(spec)
                self._publish_restore_eligibility(
                    spec=spec,
                    entity_token=agent.entity_token,
                    suppressed=False,
                )
                continue
            if bool(getattr(pac, "terminal", False)):
                self.agents.clear_restore_disposition(
                    agent.actor,
                    expected_entity_token=agent.entity_token,
                )
                self._publish_restore_eligibility(
                    spec=spec,
                    entity_token=agent.entity_token,
                    suppressed=False,
                )
                continue
            agent_policy = policy.policy_for(agent.actor)
            pending = self._pending_restore_work(agent) or bool(
                getattr(pac, "pending_work", False)
            )
            blocked = projection.block is not None
            override = (
                "pending-work"
                if pending
                else ("per-agent" if agent_policy == "always" else "none")
            )
            activity_unknown = agent.last_active_at_ms is None
            effective_activity = (
                agent.last_active_at_ms
                if agent.last_active_at_ms is not None
                else agent.created_at_ms
            )
            idle_age_ms = max(0, now_ms - effective_activity)
            inactive = is_inactive(
                effective_activity,
                now_ms=now_ms,
                threshold_ms=policy.threshold_ms,
            )
            should_restore = (
                blocked
                or override != "none"
                or (
                    agent_policy != "never"
                    and (activity_unknown or not inactive)
                )
            )
            if should_restore:
                self.agents.clear_restore_disposition(
                    agent.actor,
                    expected_entity_token=agent.entity_token,
                )
                restored.append(spec)
                if activity_unknown:
                    self._restore_activity_unknown.add(agent.actor)
                    self.agents.record_activity(agent.actor)
                self._publish_restore_eligibility(
                    spec=spec,
                    entity_token=agent.entity_token,
                    suppressed=False,
                )
                continue
            self.agents.suppress_restore(
                agent.actor,
                desired_generation=desired_generation(spec),
                last_active_at_ms=agent.last_active_at_ms,
                idle_age_ms=idle_age_ms,
                restore_threshold_ms=policy.threshold_ms,
                restore_override=(
                    "per-agent" if agent_policy == "never" else "none"
                ),
                activity_unknown=False,
            )
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=agent.entity_token,
                suppressed=True,
            )
            self._log(
                "info", "daemon", "harness.restore.idle_suppressed",
                actor=agent.uri, idleAgeMs=idle_age_ms,
                restoreThresholdMs=policy.threshold_ms,
            )
        return tuple(restored)

    def _legacy_restore_wake_running(self, operation_id, spec):
        """Observe the old CREATE object; never re-submit it as RESUME.

        None defers while an accepted legacy operation is still in progress.
        Lookup/status failures propagate to the caller's fail-closed retry path.
        A receipt alone is not proof that the worker is currently running.
        """
        try:
            receipt = self._lifecycle_manager.result(operation_id)
        except KeyError:
            return False
        if receipt.operation_id != operation_id:
            raise RuntimeError("legacy wake receipt identity mismatch")
        if receipt.state in {LifecycleState.RUNNING, LifecycleState.COMPENSATING}:
            return None
        # A terminal stop receipt may precede publication of the status view.
        # Ask the runtime owner to refresh it, using its existing bounded port;
        # neither a historical receipt nor a cached row proves current liveness.
        # Restore is the only consumer allowed a native read; the port mints a
        # one-shot capability and the runtime owner validates it.
        self._harnesses.observe_runtime_facts()
        running = any(row.get("runtime") == spec.harness and row.get("name") == spec.name
                      and row.get("running") is True for row in self._harnesses.status())
        if running and receipt.state is not LifecycleState.COMPLETED:
            self._log("warn", "daemon", "harness.restore.legacy_inconsistent",
                      actor=spec.name, operationId=operation_id)
            return None
        return running

    def _wake_dormant_agent(self, actor: str, *, reason: str) -> bool:
        agent = self.agents.get(actor)
        projection = self.agents.projection(actor)
        if (
            agent is None
            or projection is None
            or projection.restore_disposition is None
        ):
            return False
        disposition = projection.restore_disposition
        spec = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.name == agent.actor
            ),
            None,
        )
        if spec is None:
            self.agents.clear_restore_disposition(
                agent.actor,
                expected_entity_token=agent.entity_token,
                expected_desired_generation=disposition.desired_generation,
                expected_disposition_token=disposition.disposition_token,
            )
            return True
        if disposition.desired_generation != desired_generation(spec):
            cleared_stale = self.agents.clear_restore_disposition(
                agent.actor,
                expected_entity_token=agent.entity_token,
                expected_desired_generation=disposition.desired_generation,
                expected_disposition_token=disposition.disposition_token,
            )
            if cleared_stale:
                self._publish_restore_eligibility(
                    spec=spec,
                    entity_token=agent.entity_token,
                    suppressed=False,
                )
        if getattr(self, "_transfer_p2", None) is not None:
            custody = self._hydrate_transfer_source(agent.actor, agent.entity_token)
            if custody is None or custody.blocks_start:
                self._publish_restore_eligibility(
                    spec=spec, entity_token=agent.entity_token, suppressed=True,
                )
                return False
        try:
            if self._lifecycle_manager is not None:
                suffix = (f"{agent.actor}:{agent.entity_token}:"
                          f"{disposition.disposition_token}:{desired_generation(spec)}")
                legacy_running = self._legacy_restore_wake_running(
                    f"restore-wake:{suffix}", spec)
                if legacy_running is None:
                    return False
                if not legacy_running:
                    self._run_lifecycle_operation(
                        LifecycleOperation.resume(
                            f"resume-wake:{suffix}",
                            replace(self._lifecycle_spec(spec), expected_entity_token=agent.entity_token),
                        )
                    )
            elif self._harnesses is not None:
                self._harnesses.start(spec)
        except Exception as error:  # noqa: BLE001 - durable intent remains
            self._log(
                "warn",
                "daemon",
                "harness.restore.wake_failed",
                actor=agent.uri,
                reason=reason,
                errorType=type(error).__name__,
            )
            return False
        self.agents.clear_restore_disposition(
            agent.actor,
            expected_entity_token=agent.entity_token,
            expected_desired_generation=disposition.desired_generation,
            expected_disposition_token=disposition.disposition_token,
        )
        current = self.agents.projection(agent.actor)
        if current is not None and current.restore_disposition is None:
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=agent.entity_token,
                suppressed=False,
            )
        self._log(
            "info", "daemon", "harness.restore.woken",
            actor=agent.uri, reason=reason,
        )
        return True

    def _restore_wake_scan(self, _observed_at_ms: int) -> None:
        if getattr(self, "_transfer_p2", None) is not None:
            self._retry_transfer_restore()
        policy = self._restore_policy.projection()
        with self._restore_wake_lock:
            requested = tuple(self._restore_wake_requests.items())[
                : self._restore_wake_batch
            ]
        requested_actors = {actor for actor, _reason in requested}
        for actor, reason in requested:
            projection = self.agents.projection(actor)
            settled = (
                projection is None
                or projection.restore_disposition is None
                or self._wake_dormant_agent(actor, reason=reason)
            )
            if settled:
                with self._restore_wake_lock:
                    if self._restore_wake_requests.get(actor) == reason:
                        self._restore_wake_requests.pop(actor, None)
        agents = self.agents.list()
        if not agents:
            return
        start = self._restore_wake_cursor % len(agents)
        ordered = (*agents[start:], *agents[:start])
        selected = ordered[: self._restore_wake_batch]
        self._restore_wake_cursor = (start + len(selected)) % len(agents)
        if policy.degraded is not None:
            if self._restore_policy_degraded_version != policy.version:
                self._restore_policy_degraded_version = policy.version
                self._log(
                    "warn",
                    "daemon",
                    "restore-policy-degraded",
                    detail=policy.degraded,
                )
            for agent in selected:
                projection = self.agents.projection(agent.actor)
                if (
                    projection is not None
                    and projection.restore_disposition is not None
                ):
                    self._wake_dormant_agent(agent.actor, reason="policy-degraded")
            return
        for agent in selected:
            if agent.actor in requested_actors:
                continue
            projection = self.agents.projection(agent.actor)
            if projection is None or projection.restore_disposition is None:
                continue
            pac = self._pac_restore_fact(agent.actor)
            if pac is not None and bool(getattr(pac, "terminal", False)):
                cleared = self.agents.clear_restore_disposition(
                    agent.actor,
                    expected_entity_token=agent.entity_token,
                    expected_desired_generation=(
                        projection.restore_disposition.desired_generation
                    ),
                    expected_disposition_token=(
                        projection.restore_disposition.disposition_token
                    ),
                )
                if cleared:
                    self._publish_agent_restore_allow(agent)
                continue
            pending = self._pending_restore_work(agent) or bool(
                pac is not None and getattr(pac, "pending_work", False)
            )
            reason = (
                "blocked"
                if projection.block is not None
                else "pending-work"
                if pending
                else "per-agent-always"
                if policy.policy.policy_for(agent.actor) == "always"
                else None
            )
            if reason is not None:
                self._wake_dormant_agent(agent.actor, reason=reason)

    def _restore_harnesses(self) -> None:
        """Bring back every non-Lark harness this node declared.

        ``DaemonEventBridge.start`` used to do this and deliberately stopped
        (see ``test_event_bridge_start_never_restores_harness_processes``): the
        responsibility moved to whoever composes the daemon.  Nothing here took
        it up, so a restart silently left every declared connector down --
        ``contract/daemon-lifecycle`` caught it and sat red for 38 hours,
        because that contract is not in anybody's routine gate.

        Adapter restore owns the separate adapters collection. This worker
        collection contains only declared harness kinds; no kind-string
        exclusion is needed to partition restore ownership.

        U0BRACE (hq-adjutant 2026-09-04): this method is also the ONLY
        restart-time starter of non-Lark harness processes.  Construction
        used to submit one create saga per ``status == running`` row via
        ``_bootstrap_lifecycle_harnesses`` -- a predicate IDENTICAL to the
        one below -- so in steady state every such row had two submitters
        and only timing decided who started the process: the restore batch
        deferred to the saga (F1 made that settle), or the saga's claim
        landed after ``_on_restore`` had reset ``_records`` and restore
        started a SECOND process for a row the saga already owned.  That
        second submitter is gone: the startup window submits no lifecycle
        operations at all (``tests/test_daemon_restart_ownership.py``), so
        "each declared row has exactly one owner" is structural, not a
        race outcome.  Durable rows, bindings and persona routes are
        re-derived at construction from their registries; a restart only
        ever owes the PROCESSES, and it owes them here.

        Placement is after ``_start_server``: restored workers talk to the
        daemon over the IPC socket, exactly as the adapter comment above
        ``_restore_adapters`` says of adapter workers. The old call ran inside
        ``runtime.start()``, before the socket existed.

        Since the accept loop moved ahead of restore, both halves run on the
        restore thread (``_restore_then_open_gate``) and must answer
        ``stop_event`` themselves -- a shutdown can no longer wait for them
        on the main thread.
        """

        if self._harnesses is None:
            return
        if self.stop_event.is_set():
            return
        state = self.desired_state.load()
        # U0b (Allen 2026-09-03): desired state is intent + LAST KNOWN
        # RESULT.  Only rows whose last result was "running" are re-run;
        # "failed" rows (ran before, did not come back) are displayed and
        # left for a human, and rows that never earned a result do not
        # exist.  Together with start-after-success this is what makes
        # "a daemon restart never retries a start that never succeeded"
        # true.
        candidates = tuple(
            spec
            for spec in state.harnesses
            if spec.status == "running"
        )
        self._restore_classifying = True
        try:
            declared = self._classify_restore_specs(candidates)
        finally:
            self._restore_classifying = False
        # The phase-③ expectation: every declared connector owes one first
        # readiness report.  Recorded before restore runs so the maintenance
        # loop (which starts only after restore completes) never reads a
        # half-written set; keyed exactly like the actor keys its records.
        self._readiness_expected = frozenset(
            f"{spec.harness}:{spec.name}" for spec in declared
        )
        if not declared:
            self._log(
                "info",
                "daemon",
                "harness.recovery.completed",
                attempted=0,
                restored=0,
                failed=0,
            )
            return
        # A restore that fails or times out must not take the daemon with it.
        # It used to: one connector whose start never settled raised out of
        # here, and because this runs before `daemon.ready`, the whole daemon
        # aborted -- taking down every other connector, the IPC socket and the
        # bus, to punish one bad connector. Worse, the abort left the fleet
        # orphaned, and those orphans then stalled the next start the same way,
        # so the daemon could never come back without manual cleanup.
        #
        # Reconcile runs every tick once we are ready and already owns the
        # start timeout, the failure budget and the backoff, so anything
        # missed here is retried by the component whose job that is. Coming up
        # degraded and saying so beats not coming up at all.
        try:
            summary = self._harnesses.restore(declared)
        except Exception as error:  # noqa: BLE001 - startup outlives one connector
            self._log(
                "warn",
                "daemon",
                "harness.recovery.failed",
                declared=len(declared),
                errorType=type(error).__name__,
                error=str(error)[:500],
                detail=(
                    "restore did not complete; the daemon is starting without "
                    "these connectors and reconcile owns bringing them up"
                ),
            )
            # Degraded starts must be visible in the launch summary too: the
            # summary reads stderr, not daemon.jsonl.
            _mirror_startup_event_to_stderr(
                "harness.recovery.failed", declared=len(declared)
            )
            return
        self._log(
            "info",
            "daemon",
            "harness.recovery.completed",
            attempted=summary.attempted,
            restored=summary.restored,
            failed=summary.failed,
            deferred=summary.deferred,
        )

    def _start_restore_thread(
        self,
        step: Callable[[Callable[[], Any], DaemonStartupPhase], None],
    ) -> None:
        """Move restore off the startup path: accept first, restore alongside.

        The gate is cleared here, not inside the thread, so a client that
        connects between socket bind and thread start already reads
        ``phase: "restoring"`` from ping.  The thread is a daemon: a restore
        that cannot be unwound must never hold the interpreter open (the
        exit backstop exists for exactly that class of stuck thread), and
        ``_close`` gives it a bounded join before teardown proceeds.
        """

        self._restore_done.clear()
        thread = threading.Thread(
            target=self._restore_then_open_gate,
            args=(step,),
            name="hyprial-daemon-restore",
            daemon=True,
        )
        self._restore_thread = thread
        thread.start()

    def _restore_then_open_gate(
        self, step: Callable[[Callable[[], Any], DaemonStartupPhase], None]
    ) -> None:
        """Run both restore halves, then open the dispatch gate.

        Ordering inside the thread preserves the pre-thread contract:
        restore completes *before* `daemon.ready` is logged and before the
        first maintenance tick is scheduled, so reconcile still never races
        restore.  A step that escapes (only adapter/harness restore are
        wrapped, each already failure-isolated) aborts the daemon exactly
        as it did inline: `stop_event` takes the accept loop down, and
        `run()` re-raises the captured error once `_serve` returns.
        """

        try:
            step(self._restore_adapters, DaemonStartupPhase.RESTORE_ADAPTERS)
            step(self._restore_harnesses, DaemonStartupPhase.RESTORE_HARNESSES)
            step(
                self._restore_provider_auth,
                DaemonStartupPhase.RESTORE_PROVIDER_AUTH,
            )
        except BaseException as error:
            self._restore_error = error
            self._request_stop("restore-failed")
            return
        if self.stop_event.is_set():
            # Stopped mid-restore: the gate stays closed and `daemon.ready`
            # is not claimed for a daemon that is leaving.
            return
        self._restore_done.set()
        self._log("info", "daemon", "daemon.ready", nodeId=self.node_id)
        self._start_maintenance_scheduler()

    def _restore_provider_auth(self) -> None:
        """Re-open broken provider-auth episodes after a daemon restart.

        追加 1 ①: the dispatch mark blocks the failures that would re-trigger
        a relogin flow, so restore is the bounded re-trigger point.  The
        coordinator reconciles first — a credential fixed while the daemon
        was down is a recovery, not a new flow.
        """

        if self._provider_auth is not None:
            self._provider_auth.resume_after_restore()

    def _join_restore_thread(self) -> None:
        """Give the restore thread a bounded chance to leave before teardown.

        Absence of a raise is deliberate: the thread is a daemon, the loops
        it drives poll `stop_event`, and a restore that outlives this join
        is the exit backstop's case -- holding teardown hostage to it would
        recreate the "process that cannot die" class this codebase keeps
        meeting.
        """

        thread = self._restore_thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=_RESTORE_THREAD_JOIN_TIMEOUT)

    def _submit_restore_wake(
        self, actor: str | None = None, *, reason: str = "scan"
    ) -> AdmissionResult:
        if actor is not None:
            with self._restore_wake_lock:
                if (
                    actor in self._restore_wake_requests
                    or len(self._restore_wake_requests) < self._restore_wake_capacity
                ):
                    self._restore_wake_requests[actor] = reason
        if self._restore_wake_cadence is None:
            self._restore_wake_cadence = BoundedCadence(
                "restore-wake",
                self._restore_wake_scan,
            )
        return self._restore_wake_cadence.submit(time.time_ns() // 1_000_000)
