"""DaemonEventBridge core: construction, startup recovery, the reconcile timer and projection surface."""

from __future__ import annotations
from hyprial.daemon.impl.runtime.settlement import (
    HarnessActorRegistration,
    _stale_fence_rejection,
)
import json
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    InboxAuthorityUnavailable,
)
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage, InboxPruneItem
from hyprial.kernel import ReadinessReport
from hyprial.daemon.impl.api  import (
    HarnessResult,
)
from hyprial.daemon.impl.desired_state  import DesiredStateStore, InteractiveSession
from hyprial.daemon.impl.processes.supervisor  import ManagedHarnessRuntime
from hyprial.daemon.impl.dispatch.runtime.state  import (
    DispatchOwnedMap,
    DispatchOwnedSet,
    DispatchStateAuthority,
    DispatchStateProjection,
    DispatchTable,
)
from hyprial.daemon.impl.dispatch.runtime.offers  import DispatchOfferCoordinator, DispatchOfferProjection
from hyprial.daemon.impl.dispatch.runtime.holds  import HoldRefreshCoordinator, HoldRefreshProjection
from hyprial.daemon.impl.dispatch.runtime.progress  import ProgressPublishCoordinator, ProgressPublishProjection
from hyprial.daemon.impl.correlation.bounded_cadence  import BoundedCadence, CadenceCompleted
from hyprial.daemon.impl.processes.result_settlement  import ResultSettlementCoordinator, SettlementProjection
from hyprial.daemon.impl.harnesses.runtime.ports  import HarnessResultsClaimed
from hyprial.kernel import AdmissionResult
if TYPE_CHECKING:
    from hyprial.daemon.impl.inbox.contracts.api import DeliveryTransport, InboxPort
    from hyprial.daemon.impl.transport.api import PresenceView, TransportSession
    from hyprial.daemon.impl.harnesses.turn_delivery.service  import TurnHookService

from .settlement import (
    BLOCKING_FAILURE_CUSTODY_CAPACITY,
    ForwardOutcome,
    ForwardSettlementCoordinator,
    ForwardSettlementProjection,
    Forwarder,
    _InflightAttempt,
    _QueuedDeliveryHold,
)
from .forwards import _BridgeForwardsMixin
from .reconcile import _BridgeReconcileMixin
from .results import _BridgeResultsMixin


@dataclass(frozen=True, slots=True)
class DaemonRecoverySummary:
    attempted: int
    restored: int
    failed: int
    previous_run_unclean: bool
    missing_interactive: tuple[InteractiveSession, ...]


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    harness_restarts: int
    #: Retried deliveries SETTLED on this tick.  Always 0 now that retry_due
    #: runs on the delivery pump thread: settlement is counted on the pump's
    #: own ``inbox.retry_pump.completed`` event, not claimed by the tick that
    #: only kicked it.
    inbox_results: int
    harness_deliveries: int = 0
    harness_results: int = 0
    harness_progress: int = 0
    harness_orphans_retired: int = 0
    inbox_pruned_items: tuple[InboxPruneItem, ...] = ()
    # Harness keys that entered failed this tick (start-failure budget spent);
    # the daemon logs and alarms on them at the serve-loop level.
    harness_failed: tuple[str, ...] = ()
    # Phase-③ disposition reports settled since the last drain.  These are
    # "a disposition happened" events, not state entries like harness_failed:
    # every desired connector leaves exactly one per restore round, and the
    # daemon aggregates arrival + verdict without interpreting content.
    harness_readiness: tuple[ReadinessReport, ...] = ()
    # Per-phase wall time of this tick in milliseconds, for the serve-loop
    # overrun watchdog to name the culprit instead of just the total.
    phase_ms: tuple[tuple[str, int], ...] = ()
    # Sender-facing "no progress within budget" notices emitted this tick.
    # These are the loud half of the 2026-09-21 policy: the counterpart of
    # static selection is that an unproductive attempt must be reported, not
    # routed around.  Counted separately from harness_results because a
    # notice is not a settled delivery.
    availability_notices: int = 0

    @property
    def inbox_pruned(self) -> int:
        """Unconsumed inbox rows evicted by the TTL sweep this tick."""

        return len(self.inbox_pruned_items)








class _RunMarker:
    def __init__(self, path: Path) -> None:
        self.path = path
        #: When the previous marker was written, i.e. when the run that died
        #: without calling ``finish`` started.  Recovery reads it so it sweeps
        #: exactly that run's window instead of all history.
        self.previous_started_ms: int | None = None

    def begin(self) -> bool:
        previous_unclean = self.path.exists()
        self.previous_started_ms = (
            int(self.path.stat().st_mtime * 1000) if previous_unclean else None
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps({"schemaVersion": 1, "pid": os.getpid()}) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        return previous_unclean

    def finish(self) -> None:
        self.path.unlink(missing_ok=True)




class DaemonEventBridge(_BridgeForwardsMixin, _BridgeReconcileMixin, _BridgeResultsMixin):

    def __init__(
        self,
        *,
        state_dir: Path,
        node_id: str,
        desired_state: DesiredStateStore,
        transport: TransportSession,
        inbox: InboxPort,
        harnesses: ManagedHarnessRuntime,
        presence: PresenceView | None = None,
        delivery: DeliveryTransport | None = None,
        harness_actor_registrar: Callable[[str], "HarnessActorRegistration"]
        | None = None,
        harness_actor_uri: Callable[[str], str] | None = None,
        logger: Callable[..., None] | None = None,
        clock_ms: Callable[[], int] | None = None,
        usage_limit_observer: Callable[[str], None] | None = None,
        blocking_failure_observer: (
            Callable[[str, str, str], AdmissionResult | None]
            | Callable[[str, str], None] | None
        ) = None,
        blocking_failure_identity: Callable[[str], str | None] | None = None,
        blocked_actor: Callable[[str], bool] | None = None,
        workflow_outcome: Callable[[HarnessResult], bool] | None = None,
        forwarder: Forwarder | None = None,
        owner_notifier: Callable[..., object] | None = None,
        owner_requester_addresses: Collection[str] = (),
        hold_ttl_ms: int = DEFAULT_HOLD_TTL_MS,
        turn_hooks: "TurnHookService | None" = None,
        actor_mode: bool = False,
    ) -> None:
        if hold_ttl_ms <= 0:
            raise ValueError("hold TTL must be positive")
        self.state_dir = Path(state_dir)
        # Allen 2026-09-26 「提醒改发负责人」: a fail-loud notice diverted away
        # from a person's chat goes to the owner through the existing
        # owner-DM channel instead of only the log.  Called off the runtime
        # loop; never raises into it.
        self._owner_notifier = owner_notifier
        # Addresses that ALREADY reach the owner: their own squire adapter, one
        # of its routes, their ``user:`` address.  Handing a diverted notice to
        # "the owner" when the waiting sender is one of these puts the same
        # machine text in front of the same person, one wrapper deeper.
        self._owner_requester_addresses = frozenset(owner_requester_addresses)
        self.node_id = node_id
        self.desired_state = desired_state
        self.transport = transport
        self.inbox = inbox
        self.harnesses = harnesses
        # Retain the complete P1 boundary for runtime composition without
        # reaching into either concrete transport or inbox implementations.
        self.presence = presence
        self.delivery = delivery
        self.harness_actor_registrar = harness_actor_registrar
        self._logger = logger
        self._turn_hooks = turn_hooks
        # The quota watchdog hears each PROVIDER_USAGE_LIMIT turn.  It only
        # observes: settlement below is unchanged (Allen 09-17: exhausted
        # agents keep today's handling).
        self._usage_limit_observer = usage_limit_observer
        self._blocking_failure_observer = blocking_failure_observer
        self._blocking_failure_identity = blocking_failure_identity
        self._blocked_actor = blocked_actor
        self._blocking_failure_capacity = BLOCKING_FAILURE_CUSTODY_CAPACITY
        self._blocking_failure_lock = threading.Lock()
        self._pending_blocking_failures: OrderedDict[
            tuple[str, str, str], None
        ] = OrderedDict()
        self._workflow_outcome = workflow_outcome
        self._pending_workflow_results: dict[str, HarnessResult] = {}
        self._pending_workflow_attempts: dict[str, _InflightAttempt] = {}
        # Answers whose reply did not confirm on their tick, keyed by the
        # delivery they answer.  Retried as the SAME reply (idempotent) and
        # never re-dispatched meanwhile: the answer exists, asking the model
        # again is what looped wangshuo-sprite on 2026-09-24.
        self._pending_reply_results: dict[str, HarnessResult] = {}
        self._pending_forward_outcomes: dict[str, ForwardOutcome] = {}
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        # Refresh halfway through the existing hold budget.  This leaves one
        # half-budget of scheduler delay while avoiding a write on every tick.
        self._hold_refresh_interval_ms = max(1, hold_ttl_ms // 2)
        # One mapping from a supervisor-local short name to the network
        # identity.  Registration and the delivery pump must share it: keys
        # that disagree recreate the queue-forever trap this closes.
        self.harness_actor_uri = harness_actor_uri or (lambda name: name)
        self._marker = _RunMarker(self.state_dir / "daemon-run.json")
        self._mailbox_registration = None
        self._actor_registrations: dict[str, HarnessActorRegistration] = {}
        self._actor_registration_generations: dict[str, int] = {}
        self._started = False
        self._startup_in_progress = False
        self._retry_pump_kick = threading.Event()
        self._retry_pump_halt = threading.Event()
        self._retry_pump_thread: threading.Thread | None = None
        # Sender-facing fail-loud state (2026-09-21).  ``_inflight`` is keyed by
        # delivery id: a delivery belongs to exactly one worker, and the id is
        # what both progress events and harness results carry back.  Nothing
        # here influences selection -- it exists so an unproductive attempt is
        # reported instead of being routed around.
        self._inflight: dict[str, _InflightAttempt] = {}
        # Only a successful worker enqueue enters this map.  Merely seeing an
        # offline/refused delivery must not turn its ordinary TTL into a lease.
        self._queued_delivery_holds: dict[str, _QueuedDeliveryHold] = {}
        #: How many times a delivery has been handed to a worker.  A delivery
        #: id can host more than one real attempt (retry, or a second run),
        #: so the once-per-notice key must include the generation, not just the
        #: delivery id.
        self._attempt_generation: dict[str, int] = {}
        #: Notices the inbox did not accept (or could not be asked to accept).
        #: They stay here and are retried on later ticks; a failed submission
        #: is never reported as delivered.
        self._pending_notices: dict[str, InboxMessage] = {}
        self._notice_failures_logged: set[str] = set()
        # Forwards (user-proxy).  The send can post to Lark and has no bound
        # of its own, so it runs on one FIFO thread instead of the tick; one
        # thread keeps the order in which the worker finished its turns.
        # Outcomes come back through a queue and are settled on the tick, the
        # only thread that touches the fail-loud state above.
        self._forwarder = forwarder
        self._forward_jobs: queue.Queue[
            tuple[InboxMessage, HarnessResult, _InflightAttempt | None] | None
        ] = queue.Queue()
        self._forward_outcomes: queue.Queue[
            tuple[
                InboxMessage,
                HarnessResult,
                ForwardOutcome,
                _InflightAttempt | None,
            ]
        ] = queue.Queue()
        self._forward_thread: threading.Thread | None = None
        self._actor_mode = actor_mode
        self._dispatch_state: DispatchStateAuthority | None = None
        self._dispatch_offers: DispatchOfferCoordinator | None = None
        self._hold_refresh: HoldRefreshCoordinator | None = None
        self._progress_publish: ProgressPublishCoordinator | None = None
        self._result_settlement: ResultSettlementCoordinator | None = None
        self._forward_settlement: ForwardSettlementCoordinator | None = None
        self._result_claim_lane: BoundedCadence[HarnessResultsClaimed] | None = None
        self._last_result_completed = 0
        self._availability_lane: BoundedCadence[int] | None = None
        self._completed_availability_notices = 0
        self._harness_reconcile_lane: BoundedCadence[int] | None = None
        self._session_ref_lane: BoundedCadence[None] | None = None
        self._prune_lane: BoundedCadence[tuple[InboxPruneItem, ...]] | None = None
        self._cadence_results_lock = threading.Lock()
        self._completed_harness_restarts = 0

    @property
    def dispatch_state_projection(self) -> DispatchStateProjection | None:
        owner = self._dispatch_state
        return owner.projection() if owner is not None else None

    @property
    def dispatch_offer_projection(self) -> DispatchOfferProjection | None:
        owner = self._dispatch_offers
        return owner.projection() if owner is not None else None

    @property
    def hold_refresh_projection(self) -> HoldRefreshProjection | None:
        owner = self._hold_refresh
        return owner.projection() if owner is not None else None

    @property
    def progress_publish_projection(self) -> ProgressPublishProjection | None:
        owner = self._progress_publish
        return owner.projection() if owner is not None else None

    @property
    def result_settlement_projection(self) -> SettlementProjection | None:
        owner = self._result_settlement
        return owner.projection() if owner is not None else None

    @property
    def forward_settlement_projection(self) -> ForwardSettlementProjection | None:
        owner = self._forward_settlement
        return owner.projection() if owner is not None else None

    def start(self) -> DaemonRecoverySummary:
        if self._started:
            raise RuntimeError("daemon runtime is already started")
        state = self.desired_state.load()
        previous_unclean = self._marker.begin()
        # Composition below starts actors before the runtime is ready to tick.
        # A failure in that window still owns them and must make stop() drain.
        self._startup_in_progress = True
        try:
            if state.as_mailbox:
                self._mailbox_registration = self.transport.declare_liveliness(
                    f"hyprial/v1/liveliness/mailbox/{self.node_id}"
                )
        except (OSError, RuntimeError) as error:
            cleanup_errors = self._close_owned_resources()
            if cleanup_errors:
                raise ExceptionGroup(
                    "daemon startup and cleanup failed", [error, *cleanup_errors]
                ) from error
            raise
        if self._actor_mode:
            owner = DispatchStateAuthority()
            self._dispatch_state = owner
            self._inflight = DispatchOwnedMap(owner, DispatchTable.INFLIGHT)
            self._attempt_generation = DispatchOwnedMap(
                owner, DispatchTable.ATTEMPT_GENERATION
            )
            self._queued_delivery_holds = DispatchOwnedMap(
                owner, DispatchTable.QUEUED_HOLDS
            )
            self._pending_workflow_results = DispatchOwnedMap(
                owner, DispatchTable.PENDING_WORKFLOW_RESULTS
            )
            self._pending_workflow_attempts = DispatchOwnedMap(
                owner, DispatchTable.PENDING_WORKFLOW_ATTEMPTS
            )
            self._pending_reply_results = DispatchOwnedMap(
                owner, DispatchTable.PENDING_REPLY_RESULTS
            )
            self._pending_forward_outcomes = DispatchOwnedMap(
                owner, DispatchTable.PENDING_FORWARD_OUTCOMES
            )
            self._pending_notices = DispatchOwnedMap(
                owner, DispatchTable.PENDING_NOTICES
            )
            self._notice_failures_logged = DispatchOwnedSet(
                owner, DispatchTable.NOTICE_FAILURES_LOGGED
            )
            self._dispatch_offers = DispatchOfferCoordinator(
                self.harnesses.dispatch
            )
            refresh_hold = getattr(self.inbox, "refresh_hold", None)
            if callable(refresh_hold):
                self._hold_refresh = HoldRefreshCoordinator(
                    refresh_hold, owner
                )
            submit_progress = getattr(self.inbox, "submit_progress_event", None)
            if callable(submit_progress):
                self._progress_publish = ProgressPublishCoordinator(
                    submit_progress
                )
            claim_results = getattr(self.harnesses, "claim_result_batch", None)
            settle_result = getattr(self.harnesses, "settle_result", None)
            if callable(claim_results) and callable(settle_result):
                self._result_settlement = ResultSettlementCoordinator(
                    self._settle_claimed_result
                )
                if self._forwarder is not None:
                    self._forward_settlement = ForwardSettlementCoordinator(
                        self._forwarder
                    )
                self._result_claim_lane = BoundedCadence(
                    "harness-result-claim",
                    lambda _at_ms: claim_results(),
                    self._claimed_results_available,
                )
            self._harness_reconcile_lane = BoundedCadence(
                "harness-reconcile",
                lambda _at_ms: self.harnesses.reconcile(),
                self._harness_reconcile_completed,
            )
            self._session_ref_lane = BoundedCadence(
                "harness-session-refs",
                lambda _at_ms: self._sync_harness_session_refs(),
                self._session_ref_completed,
            )
            self._prune_lane = BoundedCadence(
                "inbox-prune", lambda _at_ms: self._prune_inbox(),
                self._prune_completed,
            )
            self._availability_lane = BoundedCadence(
                "availability-notices",
                lambda _at_ms: self._report_stalled_deliveries(),
                self._availability_completed,
            )
        self._started = True
        self._retry_pump_halt.clear()
        self._retry_pump_kick.clear()
        self._retry_pump_thread = threading.Thread(
            target=self._retry_pump_run,
            name="hyprial-inbox-retry-pump",
            daemon=True,
        )
        self._retry_pump_thread.start()
        self._reconcile_harness_actors()
        if previous_unclean:
            # A crash may have eaten a sender notice that was only held in
            # memory.  The terminal settlement rows are durable, so re-derive
            # from them rather than leaving that failure silent forever.
            self._recover_owed_notices(self._marker.previous_started_ms)
        self._startup_in_progress = False
        return DaemonRecoverySummary(
            attempted=0,
            restored=0,
            failed=0,
            previous_run_unclean=previous_unclean,
            missing_interactive=tuple(
                session
                for session in state.interactive_sessions
                if self.presence is None
                or not self.presence.actor_online(session.actor)
            ),
        )

    def on_timer(self, observed_at_ms: int | None = None) -> ReconcileSummary:
        """Advance event bridges after a scheduler-owned timer fires."""

        del observed_at_ms
        if not self._started:
            raise RuntimeError("daemon runtime is not started")
        phases: list[tuple[str, int]] = []

        def _timed(name: str, fn: Callable[[], Any]) -> Any:
            started_at = time.monotonic()
            try:
                return fn()
            finally:
                phases.append((name, int((time.monotonic() - started_at) * 1000)))

        _timed("agent.block.retry", self._retry_blocking_failures)

        collect_orphans = getattr(self.harnesses, "collect_orphans", None)
        harness_orphans_retired = (
            _timed("harnesses.collect_orphans", collect_orphans)
            if callable(collect_orphans)
            else 0
        )
        if self._harness_reconcile_lane is None:
            harness_restarts = _timed(
                "harnesses.reconcile", self.harnesses.reconcile
            )
            _timed("harnesses.sync_session_refs", self._sync_harness_session_refs)
        else:
            reconcile_admission = self._harness_reconcile_lane.submit(self._clock_ms())
            assert self._session_ref_lane is not None
            refs_admission = self._session_ref_lane.submit(self._clock_ms())
            if self._logger is not None:
                for domain, admission in (
                    ("harnesses.reconcile", reconcile_admission),
                    ("harnesses.sync_session_refs", refs_admission),
                ):
                    if admission is AdmissionResult.OVERLOADED:
                        self._logger(
                            "warn", "daemon", "dispatch.cadence_overloaded",
                            domain=domain,
                        )
            with self._cadence_results_lock:
                harness_restarts = self._completed_harness_restarts
                self._completed_harness_restarts = 0
        _timed("harnesses.reconcile_actors", self._reconcile_harness_actors)
        published_progress = _timed(
            "harnesses.publish_progress", self._publish_harness_progress
        )
        if self._result_claim_lane is None:
            completed_results = _timed(
                "harnesses.complete_results", self._complete_harness_results
            )
        else:
            admission = self._result_claim_lane.submit(self._clock_ms())
            if admission is AdmissionResult.OVERLOADED and self._logger is not None:
                self._logger("warn", "daemon", "harness.result_claim_overloaded")
            assert self._result_settlement is not None
            current_completed = self._result_settlement.projection().completed
            completed_results = max(0, current_completed - self._last_result_completed)
            self._last_result_completed = current_completed
        harness_deliveries = _timed(
            "harnesses.dispatch_deliveries", self._dispatch_harness_deliveries
        )
        _timed("inbox.refresh_live_holds", self._refresh_live_queued_holds)
        # The no-event loud path rides the same tick: a delivery that has
        # produced no correlated progress within its harness budget is
        # reported here, and notices the inbox did not accept are retried.
        # Runs after dispatch so a delivery that just started its first
        # attempt has its clock registered before it is judged.
        if self._availability_lane is None:
            availability_notices = _timed(
                "availability.report_stalled", self._report_stalled_deliveries
            )
        else:
            admission = self._availability_lane.submit(self._clock_ms())
            if admission is AdmissionResult.OVERLOADED and self._logger is not None:
                self._logger(
                    "warn", "daemon", "availability.cadence_overloaded"
                )
            with self._cadence_results_lock:
                availability_notices = self._completed_availability_notices
                self._completed_availability_notices = 0
        drain_failed = getattr(self.harnesses, "drain_failed_events", None)
        failed = drain_failed() if callable(drain_failed) else ()
        drain_readiness = getattr(self.harnesses, "drain_readiness_reports", None)
        readiness_reports = (
            _timed("harnesses.drain_readiness", drain_readiness)
            if callable(drain_readiness)
            else ()
        )
        inbox_results = 0
        # The retry pump owns the blocking retry_due call (it can sit on an
        # unreachable peer up to the authority timeout); the tick only kicks.
        # Completions are counted on the pump's own log event, so this tick's
        # inbox_results stays 0 rather than claiming work still in flight.
        _timed("inbox.retry_due", self._kick_retry_pump)
        # Inbox TTL sweep rides the same periodic tick as the delivery pump:
        # unconsumed rows past their deadline are evicted here, once per
        # reconcile, instead of a timer of their own.  Items are surfaced so
        # the daemon can log per-message ``inbox.pruned`` trajectory events.
        if self._prune_lane is None:
            inbox_pruned_items = _timed("inbox.prune", self._prune_inbox)
        else:
            admission = self._prune_lane.submit(self._clock_ms())
            if admission is AdmissionResult.OVERLOADED and self._logger is not None:
                self._logger("warn", "daemon", "inbox.prune_overloaded")
            assert self._dispatch_state is not None
            inbox_pruned_items = self._dispatch_state.take_pruned()
        return ReconcileSummary(
            harness_restarts=harness_restarts,
            inbox_results=inbox_results,
            harness_deliveries=harness_deliveries,
            harness_results=completed_results,
            harness_progress=published_progress,
            harness_orphans_retired=harness_orphans_retired,
            inbox_pruned_items=inbox_pruned_items,
            harness_failed=tuple(failed),
            harness_readiness=tuple(readiness_reports),
            phase_ms=tuple(phases),
            availability_notices=availability_notices,
        )

    def _harness_reconcile_completed(self, completion: CadenceCompleted[int]) -> None:
        if completion.error is not None:
            if self._logger is not None:
                self._logger(
                    "error", "daemon", "harness.reconcile_failed",
                    errorType=completion.error, detail=completion.detail,
                )
            return
        with self._cadence_results_lock:
            self._completed_harness_restarts += completion.result or 0

    def _session_ref_completed(self, completion: CadenceCompleted[None]) -> None:
        if completion.error is not None and self._logger is not None:
            self._logger(
                "warn", "daemon", "harness.session_ref.persist_failed",
                errorType=completion.error, detail=completion.detail,
            )

    def _prune_completed(
        self, completion: CadenceCompleted[tuple[InboxPruneItem, ...]]
    ) -> None:
        if completion.error is not None:
            if self._logger is not None:
                self._logger(
                    "warn", "daemon", "inbox.prune_failed",
                    errorType=completion.error, detail=completion.detail,
                )
            return
        if completion.result and self._dispatch_state is not None:
            self._dispatch_state.append_pruned(completion.result)

    def _availability_completed(self, completion: CadenceCompleted[int]) -> None:
        if completion.error is not None:
            if self._logger is not None:
                self._logger(
                    "warn", "daemon", "availability.cadence_failed",
                    errorType=completion.error, detail=completion.detail,
                )
            return
        with self._cadence_results_lock:
            self._completed_availability_notices += completion.result or 0

    def _prune_inbox(self) -> tuple[InboxPruneItem, ...]:
        try:
            return tuple(self.inbox.prune_inbox())
        except InboxAuthorityUnavailable as error:
            if not _stale_fence_rejection(error):
                raise
            # The retry pump can commit between this tick reading its fence
            # and the actor handling the prune command. The rejected sweep
            # changed nothing; the next tick reads a fresh fence and retries.
            # Mirror the pump's yield without hiding other authority failures.
            if self._logger is not None:
                self._logger(
                    "info", "daemon", "inbox.prune.yielded", detail=str(error)[:200]
                )
            return ()
