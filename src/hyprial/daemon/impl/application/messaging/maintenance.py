"""Maintenance scheduler: domain ticks, agent activity, outbox wakes, inbox watch, readiness and dispatch-gate diagnostics."""

from __future__ import annotations

from __future__ import annotations
import json
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
from hyprial.kernel import AdmissionResult, HarnessLaunchSpec, runtime_cpu_counters
from hyprial.daemon.impl.ipc.ipc_stats import owner_budget_rows
from hyprial.daemon.impl.correlation.bounded_cadence  import BoundedCadence, CadenceCompleted
from hyprial.daemon import Alarm
from hyprial.kernel import ReadinessReport
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyFacade,
    InboxMessage,
)
from hyprial.daemon.impl.transport import (
    LivelinessDirectory,
)
from hyprial.daemon.impl.inbox.contracts.api import InboxPruneItem
from hyprial.kernel import (
    parse_agent_uri,
)
from hyprial.daemon.impl.runtime  import ReconcileSummary
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.messaging.status.snapshots import (
    _WorkerStatusSnapshot,
)
from hyprial.daemon.impl.application.netendpoints.endpoints import (
    _reconcile_tick_budget,
)


_INBOX_WATCH_INTERVAL_MS = 60_000

class _BoundedRecipientWakes:
    """Bounded, duplicate-free handoff from transport callbacks to maintenance."""

    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("recipient wake capacity must be at least 1")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._recipients: set[str] = set()
        self._overflowed = False

    def record(self, recipient: str) -> None:
        with self._lock:
            if recipient in self._recipients:
                return
            if len(self._recipients) < self._capacity:
                self._recipients.add(recipient)
            else:
                self._overflowed = True

    def drain(self) -> tuple[tuple[str, ...], bool]:
        with self._lock:
            recipients = tuple(sorted(self._recipients))
            overflowed = self._overflowed
            self._recipients.clear()
            self._overflowed = False
        return recipients, overflowed

    def restore(self, recipients: tuple[str, ...], *, overflowed: bool) -> None:
        with self._lock:
            available = self._capacity - len(self._recipients)
            self._recipients.update(recipients[:available])
            self._overflowed = (
                self._overflowed or overflowed or len(recipients) > available
            )


def _record_phase_cpu(owner: str, phase: str, *, cpu_seconds: float) -> None:
    """Charge one maintenance phase; accounting must never cost the tick."""

    try:
        runtime_cpu_counters.record(owner, phase, cpu_seconds=cpu_seconds)
    except Exception:  # noqa: BLE001 - accounting never changes the outcome
        pass


class _MaintenanceMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _on_usage_refreshed(self) -> None:
        watchdog = self._quota_watchdog
        if watchdog is None:
            return
        alerts = watchdog.observe_readings()
        held = watchdog.flush_failures()
        for alert in [*alerts, *([held] if held is not None else [])]:
            self._log("info", "daemon", "quota_watchdog.alerted", kind=alert.kind, key=alert.key)

    def _transport_lock_holder(self) -> Any:
        lock_holder = getattr(getattr(self, "_transport", None), "lock_holder", None)
        return lock_holder() if lock_holder is not None else None

    def _start_maintenance_scheduler(self) -> None:
        self._maintenance_watchdog.start(self.stop_event)
        self._ensure_session_route_coordinator()
        if self._restore_wake_cadence is None:
            self._restore_wake_cadence = BoundedCadence(
                "restore-wake",
                self._restore_wake_scan,
            )
        if self._dispatch_cadence is None:
            self._dispatch_cadence = BoundedCadence(
                "dispatch-runtime",
                self._runtime_timer,
                self._on_dispatch_tick_completed,
            )
        if self._forwarding_cadence is None and self._forwarding_supervisor is not None:
            self._forwarding_cadence = BoundedCadence(
                "forwarding-reconcile",
                lambda _observed_at_ms: self._reconcile_forwarding_endpoints(),
                self._on_forwarding_tick_completed,
            )
        self._maintenance_generation += 1
        self._schedule_maintenance(self._maintenance_generation, delay=1.0)

    def _on_dispatch_tick_completed(
        self, completion: CadenceCompleted[ReconcileSummary]
    ) -> None:
        if completion.error is not None:
            self._log(
                "error", "daemon", "daemon.reconcile_failed",
                phase="runtime.timer",
                errorType=completion.error,
                detail=completion.detail or completion.error,
                durationMs=completion.duration_ms,
            )
            return
        if completion.result is not None:
            self._record_maintenance_outcome(
                completion.started_at, completion.result, (), 0,
                (("runtime.timer", completion.duration_ms),),
            )

    def _on_forwarding_tick_completed(
        self, completion: CadenceCompleted[None]
    ) -> None:
        if completion.error is not None:
            self._log(
                "error", "daemon", "forwarding.reconcile_failed",
                errorType=completion.error,
                detail=completion.detail or completion.error,
                durationMs=completion.duration_ms,
            )

    def _schedule_maintenance(self, generation: int, *, delay: float) -> None:
        if self.stop_event.is_set():
            return
        self._maintenance_scheduler.schedule(
            "daemon-maintenance",
            generation,
            delay,
            self._scheduled_maintenance,
        )

    def _scheduled_maintenance(self, generation: int) -> None:
        if (
            generation != self._maintenance_generation
            or self.stop_event.is_set()
        ):
            return
        started = time.monotonic()
        watchdog = self._maintenance_watchdog
        watchdog.tick_started()
        try:
            watchdog.phase("agent-activity")
            phase_cpu = time.thread_time()
            try:
                self._flush_agent_activity()
                self._retry_restore_eligibility()
                if self._restore_wake_cadence is not None:
                    admission = self._restore_wake_cadence.submit(
                        time.time_ns() // 1_000_000
                    )
                    if admission is AdmissionResult.OVERLOADED:
                        self._log("warn", "daemon", "restore.wake_tick_overloaded")
            finally:
                _record_phase_cpu(
                    "daemon-maintenance",
                    "agent-activity",
                    cpu_seconds=time.thread_time() - phase_cpu,
                )
            outcome, adapter_events, adapter_restarts, phases = (
                self._run_scheduled_domains(started)
            )
            watchdog.phase("record-outcome")
            phase_cpu = time.thread_time()
            try:
                self._record_maintenance_outcome(
                    started,
                    outcome,
                    adapter_events,
                    adapter_restarts,
                    phases,
                )
            finally:
                _record_phase_cpu(
                    "daemon-maintenance",
                    "record-outcome",
                    cpu_seconds=time.thread_time() - phase_cpu,
                )
        except Exception as error:  # noqa: BLE001 - maintenance isolation boundary
            self._log(
                "error",
                "daemon",
                "daemon.reconcile_failed",
                errorType=type(error).__name__,
                detail=str(error)[:500],
            )
        finally:
            # Its own boundary: a failed tick must not skip the writer check,
            # and a failed check must not cost the tick (review 821).
            try:
                watchdog.phase("state-writer-alarm")
                phase_cpu = time.thread_time()
                try:
                    self._check_state_writer_latency()
                finally:
                    _record_phase_cpu(
                        "daemon-maintenance",
                        "state-writer-alarm",
                        cpu_seconds=time.thread_time() - phase_cpu,
                    )
            except Exception as error:  # noqa: BLE001 - alarm isolation boundary
                self._log_trace(
                    "error", "daemon.state_writer.alarm_failed",
                    errorType=type(error).__name__, detail=str(error)[:500],
                )
            finally:
                try:
                    watchdog.phase("cpu-owner-alarm")
                    phase_cpu = time.thread_time()
                    try:
                        self._check_cpu_owner_budget()
                    finally:
                        _record_phase_cpu(
                            "daemon-maintenance",
                            "cpu-owner-alarm",
                            cpu_seconds=time.thread_time() - phase_cpu,
                        )
                except Exception as error:  # noqa: BLE001 - alarm isolation boundary
                    self._log_trace(
                        "error", "daemon.cpu.owner_alarm_failed",
                        errorType=type(error).__name__, detail=str(error)[:500],
                    )
                finally:
                    # Rescheduling is the loop itself: nothing above may skip it.
                    try:
                        watchdog.tick_finished()
                    finally:
                        self._schedule_maintenance(generation, delay=1.0)

    def _queue_agent_activity(self, actor: str) -> None:
        """Enqueue without waiting; the maintenance owner performs SQLite I/O."""

        self._agent_activity_queue.put(actor)

    def _bind_outbox_recipient_wake(
        self,
        directory: LivelinessDirectory,
        inbox: DeliveryCustodyFacade,
    ) -> None:
        """Keep the transport callback lane independent of inbox authority."""

        del inbox
        directory.set_actor_online_callback(self._outbox_recipient_wakes.record)

    def _flush_outbox_recipient_wakes(self, *, now_ms: int) -> None:
        """Apply callback-recorded wakes from the maintenance owner."""

        inbox = self._inbox
        if inbox is None:
            return
        recipients, overflowed = self._outbox_recipient_wakes.drain()
        if not recipients and not overflowed:
            return
        if overflowed:
            try:
                recipients = tuple(
                    sorted(
                        set(recipients).union(
                            item.message.recipient for item in inbox.outbox_items()
                        )
                    )
                )
            except Exception:
                self._outbox_recipient_wakes.restore(
                    recipients,
                    overflowed=True,
                )
                raise
        for index, recipient in enumerate(recipients):
            try:
                inbox.wake_outbox_recipient(recipient, now_ms=now_ms)
            except Exception:
                self._outbox_recipient_wakes.restore(
                    recipients[index:],
                    overflowed=False,
                )
                raise

    def _wake_pending_dormant_agents(self) -> None:
        for agent in self.agents.list():
            if self._agent_registry.restore_disposition(agent.actor) is None:
                continue
            if self._pending_restore_work(agent):
                self._wake_dormant_agent(agent.actor, reason="pending-work")

    def _flush_agent_activity(self) -> None:
        pending: set[str] = set()
        while True:
            try:
                pending.add(self._agent_activity_queue.get_nowait())
            except queue.Empty:
                break
        for actor in pending:
            try:
                self.agents.record_activity(actor)
            except Exception as error:  # noqa: BLE001 - activity is best effort
                self._log(
                    "warn",
                    "daemon",
                    "agent.activity_write_failed",
                    actor=actor,
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )

    def _check_state_writer_latency(self) -> None:
        """Alarm when the single state writer stays saturated or backs up."""

        persistence = getattr(self, "_state_persistence", None)
        alarm = getattr(self, "_state_writer_alarm", None)
        if persistence is None or alarm is None:
            return
        outcome = alarm.observe(persistence.latency_status())
        if outcome is None:
            return
        kind, fields = outcome
        fields = dict(fields)
        page = bool(fields.pop("_page", True))
        if kind == "recovered":
            self._log("info", "daemon", "daemon.state_writer.recovered", **fields)
            return
        self._log("error", "daemon", "daemon.state_writer.slow", **fields)
        if not page:
            return
        # Nobody reads daemon.jsonl during an incident: also page the owner
        # through the operator alarm path (it runs on the inbox authority, not
        # on the saturated state writer).
        # A fixed reason lets the inbox collapse repeats; the numbers go in
        # the human-readable text.
        self._raise_harness_failed_alarm(
            str(int(time.time())),
            kind="state-writer-slow",
            reason="STATE_WRITER_SLOW",
            text=", ".join(f"{key}={value}" for key, value in fields.items()),
        )

    def _check_cpu_owner_budget(self) -> None:
        alarm = getattr(self, "_cpu_owner_alarm", None)
        if alarm is None:
            return
        # Every owner, including those with a legacy ipcStats side that the
        # generic runtime block excludes (the #1173 hot spot was one).
        rows = owner_budget_rows(self._ipc_stats_snapshot())
        for fields in alarm.observe(rows):
            self._log(
                "error",
                "daemon",
                "daemon.cpu.owner_over_budget",
                **fields,
            )

    def _record_maintenance_outcome(
        self,
        started: float,
        outcome: Any,
        adapter_events: tuple[Any, ...],
        adapter_restarts: int,
        phases: tuple[tuple[str, int], ...],
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        budget = _reconcile_tick_budget()
        if budget > 0 and duration_ms > int(budget * 1000):
            sub_phases = [
                (f"runtime.{name}", ms)
                for name, ms in getattr(outcome, "phase_ms", ())
            ]
            candidates = [
                entry
                for entry in phases
                if not (sub_phases and entry[0] == "runtime.timer")
            ] + sub_phases
            item = max(candidates, key=lambda entry: entry[1])[0] if candidates else "unknown"
            # The whole tick paid the overrun, so name the worst offenders,
            # not just the single slowest step: the old one-item form read as
            # "everything timed out" whenever one step did (2026-09-14).
            top = sorted(candidates, key=lambda entry: entry[1], reverse=True)[:3]
            self._log(
                "error",
                "daemon",
                "daemon.reconcile_overrun",
                durationMs=duration_ms,
                budgetMs=int(budget * 1000),
                item=item,
                items=[{"item": name, "ms": ms} for name, ms in top],
            )
        for failed in getattr(outcome, "harness_failed", ()):
            self._log(
                "error",
                "daemon",
                "daemon.harness.failed",
                harness=failed,
                detail="harness retry budget exhausted; explicit start is required",
            )
            self._raise_harness_failed_alarm(failed)
        self._aggregate_readiness_reports(getattr(outcome, "harness_readiness", ()))
        for adapter_event in adapter_events:
            fields = dict(adapter_event)
            event = str(fields.pop("event", "lark.inbound.health"))
            level = (
                "error"
                if event in {"lark.inbound.stale", "lark.adapter.lifecycle.timeout"}
                else "warn"
                if event
                in {"lark.pin.query_failed", "lark.identity.lookup_failed"}
                else "info"
            )
            self._log(level, "lark-adapter", event, **fields)
        if (
            getattr(outcome, "harness_restarts", 0)
            or getattr(outcome, "inbox_results", 0)
            or adapter_restarts
        ):
            self._log(
                "info",
                "daemon",
                "daemon.reconciled",
                harnessRestarts=getattr(outcome, "harness_restarts", 0),
                inboxResults=getattr(outcome, "inbox_results", 0),
                inboxPruned=getattr(outcome, "inbox_pruned", 0),
                adapterRestarts=adapter_restarts,
            )
        for item in getattr(outcome, "inbox_pruned_items", ()):
            self._record_pruned_workflow_request(item)
            self._log(
                "info",
                "daemon",
                "inbox.pruned",
                messageId=item.message_id,
                correlationId=item.message_id,
                node="delivery-terminal",
                recipient=item.recipient,
                reason=item.reason,
                createdAtMs=item.created_at_ms,
                receivedAtMs=item.received_at_ms,
            )
        if outcome is not None:
            self._watch_inbox_collection(outcome.inbox_pruned_items)

    def _record_pruned_workflow_request(self, item: InboxPruneItem) -> None:
        """Route an inbox prune to the local or remote PAC authority."""

        try:
            workflow = getattr(self, "_workflow_service", None)
            handled = bool(
                workflow
                and workflow.record_request_pruned(
                    message_id=item.message_id,
                    recipient=item.recipient,
                )
            )
            remote = getattr(self, "_remote_workflow", None)
            if not handled and remote is not None:
                handled = remote.request_pruned(item.message_id, item.recipient)
            if handled:
                self._log(
                    "info",
                    "pac",
                    "workflow.request_pruned",
                    messageId=item.message_id,
                    recipient=item.recipient,
                    reason="pac:request-expired",
                )
        except Exception as error:  # noqa: BLE001 - preserve daemon reconcile
            self._log(
                "error",
                "pac",
                "workflow.request_prune_failed",
                messageId=item.message_id,
                recipient=item.recipient,
                exceptionClass=type(error).__name__,
                detail=str(error),
            )

    def _watch_inbox_collection(
        self, pruned: tuple[InboxPruneItem, ...] = ()
    ) -> None:
        """Report mail nobody is collecting -- before and after the deadline.

        Both readings need the same liveness answer, so they share one
        snapshot: the periodic "nobody has taken this" check runs at most
        once per ``_INBOX_WATCH_INTERVAL_MS`` because it is a GROUP BY over
        the whole inbox (129 MB on the production node), while the
        prune-time check rides the sweep that just produced its input.
        """

        watchdog = self._inbox_watchdog
        if watchdog is None:
            return
        now_ms = time.time_ns() // 1_000_000
        due = now_ms - self._inbox_watchdog_checked_at_ms >= _INBOX_WATCH_INTERVAL_MS
        lost = tuple(
            (item.recipient, item.message_id)
            for item in pruned
            if item.reason == "TTL_EXPIRED"
        )
        if not due and not lost:
            return
        live = self._running_actor_uris()
        alerts = []
        try:
            if lost:
                alerts += watchdog.observe_pruned(lost, is_running=live.__contains__)
            if due:
                self._inbox_watchdog_checked_at_ms = now_ms
                stats = getattr(self._inbox, "unfetched_recipient_stats", None)
                if callable(stats):
                    alerts += watchdog.observe_unfetched(
                        stats(), is_running=live.__contains__
                    )
        except Exception as error:  # noqa: BLE001 - a watchdog never breaks the tick
            self._log(
                "error",
                "daemon",
                "inbox_watchdog.failed",
                error=f"{type(error).__name__}: {error}",
            )
            return
        for alert in alerts:
            self._log(
                "warn", "daemon", "inbox_watchdog.alerted", kind=alert.kind, key=alert.key
            )

    def _aggregate_readiness_reports(
        self, reports: tuple[ReadinessReport, ...]
    ) -> None:
        """Fold one tick's disposition reports into the daemon.readiness projection.

        Two questions only: did the report arrive, and what verdict did it
        carry.  First arrival per connector and the first complete round are
        different observations and are logged under different events
        (``daemon.readiness.report`` / ``daemon.readiness.reconciled``).  A
        verdict outside the known set is recorded verbatim, never branched
        on: the schema contract is the three fields, the vocabulary grows
        with reporters, and an aggregator that interprets content would be
        claiming a knowledge the design deliberately withholds from it.
        """

        for report in reports:
            if report.source in self._readiness_reports:
                self._readiness_reports[report.source] = report
                continue
            self._readiness_reports[report.source] = report
            self._log(
                "info",
                "daemon",
                "daemon.readiness.report",
                connector=report.source,
                phase=report.phase,
                verdict=report.verdict,
            )
        if (
            not self._readiness_first_round.is_set()
            and self._readiness_expected <= self._readiness_reports.keys()
        ):
            self._readiness_first_round.set()
            # A histogram, not a branch: failure must look different from
            # success in the record, and counting per verdict value is the
            # whole of what the daemon may do with the content.
            tally: dict[str, int] = {}
            for key in sorted(self._readiness_expected):
                verdict = self._readiness_reports[key].verdict
                tally[verdict] = tally.get(verdict, 0) + 1
            self._log(
                "info",
                "daemon",
                "daemon.readiness.reconciled",
                expected=len(self._readiness_expected),
                verdicts=tally,
            )

    def _run_scheduled_domains(
        self, now: float
    ) -> tuple[Any, tuple[Any, ...], int, tuple[tuple[str, int], ...]]:
        """Submit one scheduler-owned cadence event to each domain.

        Client reads are concurrent, so the dispatcher no longer implicitly
        orders maintenance against request handling. Route expiry must not act
        on a stale observation after a heartbeat has already returned alive;
        runtime and adapter reconciliation likewise mutate daemon-owned state.
        """

        assert self._runtime is not None
        phases: list[tuple[str, int]] = []

        def _timed(name: str, fn: Callable[[], Any]) -> Any:
            self._maintenance_watchdog.phase(name)
            started_at = time.monotonic()
            started_cpu = time.thread_time()
            try:
                return fn()
            finally:
                phases.append((name, int((time.monotonic() - started_at) * 1000)))
                _record_phase_cpu(
                    "daemon-maintenance",
                    name,
                    cpu_seconds=time.thread_time() - started_cpu,
                )

        _timed("routes.expire", lambda: self._expire_stale_channel_routes(now))
        if now >= self._profile_refresh_due:
            self._profile_refresh_due = now + 30.0
            try:
                self.user_profiles.refresh(wait=False)
            except Exception as error:
                self._log(
                    "warn", "daemon", "profile.refresh_overloaded",
                    errorType=type(error).__name__,
                )
        if now >= self._keep_refresh_due:
            self._keep_refresh_due = now + 30.0
            try:
                self._agent_keep.refresh(wait=False)
            except Exception as error:
                self._log(
                    "warn", "daemon", "agent.keep_refresh_overloaded",
                    errorType=type(error).__name__,
                )
            try:
                self._restore_policy.refresh(wait=False)
            except Exception as error:
                self._log(
                    "warn",
                    "daemon",
                    "restore.policy_refresh_overloaded",
                    errorType=type(error).__name__,
                )
        forwarding = self._forwarding_cadence
        if forwarding is None:
            _timed("forwarding.timer", self._reconcile_forwarding_endpoints)
        else:
            admission = forwarding.submit(int(time.time_ns() // 1_000_000))
            if admission is AdmissionResult.OVERLOADED:
                self._log("warn", "daemon", "forwarding.tick_overloaded")
        observed_at_ms = int(time.time_ns() // 1_000_000)
        cadence = self._dispatch_cadence
        if cadence is None:
            outcome = _timed("runtime.timer", lambda: self._runtime_timer(observed_at_ms))
        else:
            admission = cadence.submit(observed_at_ms)
            if admission is AdmissionResult.OVERLOADED:
                self._log("warn", "daemon", "dispatch.tick_overloaded")
            outcome = None

        # Timer admission is bounded and never shares ownership with local IPC.
        # Each domain actor serializes its own cadence with its business commands.
        if self._workflow_service is not None:
            _timed(
                "workflow.timer",
                lambda: self._workflow_service.submit_timer(observed_at_ms),
            )
        if self._routine_service is not None:
            _timed(
                "routine.timer",
                lambda: self._routine_service.submit_timer(observed_at_ms),
            )
        if self._pac_actor_service is not None:
            _timed("pac.actor.timer", self._pac_actor_service.submit_tick)
        orgfs = self._orgfs_runtime
        if orgfs is not None:  # the tick never raises: it logs and returns 0
            _timed("orgfs.anti_entropy", lambda: orgfs.anti_entropy_tick(now))

        adapter_events = (
            _timed("adapters.drain_health", self._lark_client.drain_health_events)
            if self._lark_client
            else ()
        )
        adapter_restarts = (
            int(
                _timed(
                    "adapters.timer",
                    lambda: self._lark_client.timer(
                        int(time.time_ns() // 1_000_000)
                    ),
                ).changed
            )
            if self._lark_client
            else 0
        )
        return outcome, adapter_events, adapter_restarts, tuple(phases)

    def _raise_harness_failed_alarm(
        self, key: str, *, kind: str = "harness-failed", reason: str | None = None,
        text: str | None = None,
    ) -> None:
        """Best-effort operator alarm for one harness whose budget ran out.

        ``kind``/``reason`` reuse the same channel for other operator alarms
        (the saturated state writer), keeping one emitter access site.

        Failed is terminal -- nothing retries it again -- so this alarm is the
        only path that gets a human involved.  The emitter is already a
        non-raising, non-recursive boundary; when no delivery channel is
        configured the trajectory events
        (``alarm.raised``/``alarm.failed``) are still written, which is the
        guaranteed-loud part.
        """

        inbox = self._inbox
        emitter = getattr(inbox, "alarm_emitter", None) if inbox is not None else None
        if emitter is None:
            return
        alarm = Alarm(
            correlation_id=f"{kind}:{key}",
            message_id=f"{kind}:{key}",
            conversation_id=(
                f"daemon-harness:{self.node_id}"
                if kind == "harness-failed"
                else f"daemon-{kind}:{self.node_id}"
            ),
            sender=self.node_id,
            recipient=self.owner,
            reason=reason or f"HARNESS_FAILED:{key}",
            audience="operator",
            text=text,
        )
        try:
            result = emitter.emit(alarm, terminal=False)
        except Exception:  # noqa: BLE001 - alarming must never hurt the loop
            return
        # The owner DM rides on the emitter's durable throttle claim. A failed
        # node notice still pages when it failed after that claim; a pre-claim
        # facade failure does not, or every maintenance tick could page again.
        if result.status != "delivered" and not result.claimed:
            return
        delivery = getattr(self, "_operator_alarm_delivery", None)
        if delivery is None:
            return
        harness = None
        if kind == "harness-failed":
            try:
                harness = self._failed_harness_spec(key)
            except Exception:  # noqa: BLE001 - lookup failure must not cost the DM
                harness = None
        try:
            delivery.submit(
                alarm,
                kind=kind,
                key=key,
                owner=self.owner,
                node=self.node_id,
                harness=harness,
            )
        except Exception:  # noqa: BLE001 - alarming must never hurt the loop
            pass

    def _failed_harness_spec(self, key: str) -> HarnessLaunchSpec | None:
        """The desired harness launch spec this whole failure key names.

        Matched by comparing whole keys, never by splitting one: addresses
        are decomposed only in the uri module (test_address_parsing_guard).
        """

        store = getattr(self, "desired_state", None)
        if store is None:
            return None
        for spec in store.load().harnesses:
            if f"{spec.harness}:{spec.name}" == key:
                return spec
        return None

    @staticmethod
    def _routine_coordinator_marker(name: str, registration_id: object = None) -> str:
        prefix = f"routine:{name}:{registration_id}" if registration_id else f"routine:{name}"
        return f"{prefix}:coordinator"

    def _dispatch_without_pac_snapshot(self) -> int:
        """Dispatch-without-PAC count for this daemon epoch (ps/top read this)."""

        diagnostics = self._dispatch_diagnostics
        if diagnostics is not None:
            return diagnostics.projection().without_pac
        with self._dispatch_without_pac_lock:
            return self._dispatch_without_pac_count

    def _dispatch_conversation_snapshot(self) -> int:
        """Conversation-classified count, same epoch scope, for contrast.

        The denominator-next-door the 2026-09-04 reading needed: ps/top
        show it beside dispatchWithoutPacCount so an operator can see the
        gate is still observing the request-shape traffic it stopped
        counting.
        """

        diagnostics = self._dispatch_diagnostics
        if diagnostics is not None:
            return diagnostics.projection().conversation
        with self._dispatch_without_pac_lock:
            return self._dispatch_conversation_count

    _DISPATCH_FEATURE_KEYWORDS = ("交付物", "分支", "spec", "首个可核产出")

    def _is_dispatch_candidate(
        self, *, sender: str, recipient: str, intent: str
    ) -> bool:
        """The request-intent §三② shape: a dispatch or conversation candidate.

        Four clauses, each one spec term:

        • 非 workflow 发出 -- PAC deliveries never traverse message.send at
          all (they enter the inbox directly under ``workflow:{effect_id}``
          idempotency keys), so this boundary sees only non-workflow sends;
          the sender clause below is the defensive backstop for the one
          actor that must never count even if a future path routes it here.
        • intent=request -- replies (``replyTo``) are answers, not dispatches.
        • 发件方是协调者/人 -- every sender that survives the resolve-or-reject
          boundary is a registered local agent (coordinator), a ``user:``
          human, or an adapter-forwarded human; all of them dispatch.
        • 收件方是本机 agent -- a canonical ``agent:<owner>:<node>:<name>``
          URI naming THIS node; user/route targets and foreign-node agents
          are not workers being assigned work here.

        spec-dispatch-gate-classifier-2026-09-04 keeps this shape unchanged
        as the CANDIDATE set; which candidates count is decided by
        ``_classify_dispatch_send``.
        """

        if intent != "request":
            return False
        if sender == self._dispatch_service_actor:
            return False
        parsed = parse_agent_uri(recipient)
        if parsed is None or parsed[0] != self.owner or parsed[1] != self.node_id:
            return False
        return True

    def _dispatch_send_has_feature(self, message: InboxMessage) -> bool:
        """Condition (b): the body or payload carries a dispatch feature.

        Either the 正文 contains one of the PAC template delivery-section
        keywords, or the payload dict carries a ``task``/``deliverable``
        field.  message.send builds its own ``{"message", "topic"}`` payload
        today, so the field disjunct is the forward-compatible half; both
        are spec terms, kept verbatim.
        """

        try:
            payload = json.loads(message.payload) if message.payload else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if "task" in payload or "deliverable" in payload:
            return True
        text = payload.get("message")
        if isinstance(text, str):
            return any(
                keyword in text for keyword in self._DISPATCH_FEATURE_KEYWORDS
            )
        return False

    def _classify_dispatch_send(
        self, message: InboxMessage, *, conversation_is_new: bool
    ) -> bool:
        """The narrowed judgment: count only new-conversation dispatch shapes.

        spec-dispatch-gate-classifier-2026-09-04: a candidate counts as
        ``dispatch.without_pac`` only when BOTH hold —

        * (a) 新会话: this send opens its conversation (computed at the
          message.send boundary, before any target loop);
        * (b) 派活特征: delivery-section keyword or task/deliverable field.

        Every other candidate is a conversation: ``dispatch.conversation``
        event plus the contrast counter, never the numerator.  Still
        record-only — this returns whether it counted, changes nothing else
        about the delivery.
        """

        if conversation_is_new and self._dispatch_send_has_feature(message):
            self._record_dispatch_without_pac(message)
            return True
        self._record_dispatch_conversation(
            message,
            reason=(
                "existing-conversation"
                if not conversation_is_new
                else "no-dispatch-feature"
            ),
        )
        return False

    def _record_dispatch_without_pac(self, message: InboxMessage) -> None:
        """Emit the durable event and bump the ps/top counter.  Never blocks.

        A replayed idempotent send re-traverses this path and logs again under
        the same messageId: the daemon.jsonl stream collapses on messageId,
        the epoch counter deliberately does not (it counts send attempts that
        dispatched, matching what a daemon sees).
        """

        self._log(
            "info",
            "daemon",
            "dispatch.without_pac",
            sender=message.sender,
            recipient=message.recipient,
            messageId=message.message_id,
        )
        diagnostics = self._dispatch_diagnostics
        if diagnostics is not None:
            try:
                diagnostics.classify(True)
            except TimeoutError:
                self._log("warn", "daemon", "dispatch.diagnostic_overloaded")
        else:
            with self._dispatch_without_pac_lock:
                self._dispatch_without_pac_count += 1

    def _record_dispatch_conversation(
        self, message: InboxMessage, *, reason: str
    ) -> None:
        """Emit dispatch.conversation for an uncounted candidate.  Never blocks.

        The observable residue of the narrowed gate (spec §1): every
        request-shape send the gate does not count stays inspectable in
        daemon.jsonl with WHY it was not counted, and the ps/top contrast
        counter shows the daemon still sees this traffic.
        """

        self._log(
            "info",
            "daemon",
            "dispatch.conversation",
            sender=message.sender,
            recipient=message.recipient,
            messageId=message.message_id,
            reason=reason,
        )
        diagnostics = self._dispatch_diagnostics
        if diagnostics is not None:
            try:
                diagnostics.classify(False)
            except TimeoutError:
                self._log("warn", "daemon", "dispatch.diagnostic_overloaded")
        else:
            with self._dispatch_without_pac_lock:
                self._dispatch_conversation_count += 1

    def _current_worker_snapshot(self) -> _WorkerStatusSnapshot | None:
        """The snapshot installed for this request thread, when there is one."""

        return getattr(self._worker_snapshot_local, "current", None)

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self._logger.bind(component=component).log(level, event, **fields)

    def _log_trace(self, level: str, event: str, **fields: Any) -> None:
        """Log a step marker, accepting that the write itself may fail.

        These events exist only to say which step is running.  Every one costs
        an ``os.open``, so under fd exhaustion the instrumentation becomes the
        thing that fails -- and a daemon that cannot start *because* it is
        describing its own startup is a worse failure than the silence this
        was added to fix.  ``test_real_low_fd_pressure_recovers_without_
        restarting_daemon`` holds that line: the daemon has to survive fd
        pressure, and it may not be a step marker that stops it.

        Only for markers.  Anything a caller must act on keeps using
        ``_log``, where a lost write is a real defect rather than a
        degradation.
        """

        try:
            self._log(level, "daemon", event, **fields)
        except OSError:
            pass
