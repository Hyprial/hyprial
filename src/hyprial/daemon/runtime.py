"""Daemon lifecycle: storage, mailbox role, inbox and managed harnesses."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Protocol
from uuid import NAMESPACE_URL, uuid5

from hyprial.inbox import InboxAuthorityTimeout, InboxAuthorityUnavailable
from hyprial.inbox.api import DeliveryLifecycle, InboxMessage, InboxPruneItem
from hyprial.inbox.progress import COALESCE_KEPT_PHASES, ProgressEvent
from hyprial.contracts.readiness import ReadinessReport

from hyprial.uri import parse_agent_uri
from hyprial.availability_loud import (
    AttemptIdentity,
    is_human_facing_requester,
    no_progress_budget_seconds,
    no_progress_notice,
    unavailable_notice,
)

from .api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
    classify_harness_failure,
    harness_failure_is_permanent,
)
from .desired_state import DesiredStateError, DesiredStateStore, InteractiveSession
from .supervisor import ManagedHarnessRuntime

if TYPE_CHECKING:
    from hyprial.inbox.api import DeliveryTransport, InboxPort
    from hyprial.transport.api import PresenceView, TransportSession


HARNESS_FAILURE_MAX_ATTEMPTS = 3
HARNESS_FAILURE_BACKOFF_MS = (1_000, 5_000)
#: Settlement code when a harness asks for a forward on a daemon that was
#: composed without a forwarder.  Not permanent: it is a wiring fault that a
#: restart fixes, and the sender hears about every attempt.
FORWARD_UNAVAILABLE = "FORWARD_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """What sending one forward AS the worker produced.

    ``accepted`` means the send boundary took the message (queued for an
    agent, or posted for a route).  Otherwise ``failure_code`` is the stable
    code the delivery is settled with.
    """

    accepted: bool
    failure_code: str | None = None
    error: str | None = None


#: ``(original inbox row, to, text) -> outcome``.  Supplied by the
#: application, which owns the one send boundary (agent/route/user targets);
#: the runtime only decides WHEN and settles the original row afterwards.
Forwarder = Callable[[InboxMessage, str, str], ForwardOutcome]


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


#: Fallback cadence for the inbox retry pump when no tick kick arrives (the
#: reconcile tick normally nudges it once per second).  Delivery-pump timing,
#: not supervision: no actor lifecycle capability is re-implemented here.
_RETRY_PUMP_IDLE_SECONDS = 5.0

#: How long stop() waits for the pump to leave a blocking ``retry_due`` call.
#: Derived, not chosen: the facade's authority timeout (2 s, the ``timeout``
#: default of DeliveryCustodyFacade in inbox/authority.py) bounds that call,
#: and this outlasts it; a pump that still will not leave is a daemon thread
#: and is reported, never joined forever.
_RETRY_PUMP_JOIN_SECONDS = 3.0

#: A retry pass slower than this is worth its own log line even when it
#: settled nothing: head-of-line blocking on one unreachable target is
#: otherwise invisible until the next incident (2026-09-14).
_RETRY_PUMP_SLOW_MS = 1000


def _stale_fence_rejection(error: BaseException) -> bool:
    """A ``retry_due`` command whose generation/version fence moved under it.

    The authority client reads the fence, then submits; since retry_due left
    the tick thread, the tick's own inbox commands can commit a version in
    that window and the actor rejects the stale fence as
    ``InboxAuthorityUnavailable("STALE_COMMAND: ...")``.  That is the
    expected loser of a race the two threads are supposed to run, not a
    fault: the pass yields and the next kick re-reads the fence.  Only the
    ``STALE_COMMAND`` raise shape matches here -- the admission-failure
    shape (``inbox command admission failed: ...``) stays a pump failure.
    """

    return isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
        "STALE_COMMAND"
    )


def _reply_already_answered(error: BaseException) -> bool:
    """A reply submit refused because this delivery's reply is already durable.

    ``_reply_and_ack`` derives the reply id from the delivery it answers, so
    the authority's ``SUBMISSION_RECEIPT_CONFLICT`` on it means a reply with
    different text already committed (typically: the first submit timed out
    after the reply was sent, and a re-run turn answered again).  Same raise
    shape as ``_stale_fence_rejection``.
    """

    return isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
        "SUBMISSION_RECEIPT_CONFLICT"
    )


def _coalesce_progress_events(events: tuple[ProgressEvent, ...]) -> tuple[ProgressEvent, ...]:
    """Keep the newest tool-call, tool-result, and other event per delivery.

    Coalesced events count as drops, and their own ``dropped_since_seq``
    accounting carries forward, so consumers can still tell exactly how much
    producer-side information was elided during this tick.
    """

    grouped: dict[tuple[str, str], list[ProgressEvent]] = {}
    for event in events:
        grouped.setdefault((event.actor, event.delivery_id), []).append(event)
    output: list[ProgressEvent] = []
    for group in grouped.values():
        retained_by_bucket: dict[str, ProgressEvent] = {}
        for event in group:
            bucket = event.phase if event.phase in COALESCE_KEPT_PHASES else "other"
            retained_by_bucket[bucket] = event
        retained = sorted(retained_by_bucket.values(), key=lambda item: item.seq)
        retained_ids = {id(item) for item in retained}
        merged = [item for item in group if id(item) not in retained_ids]
        merged_count = len(merged) + sum(item.dropped_since_seq for item in merged)
        if retained and merged_count:
            retained[0] = replace(
                retained[0],
                dropped_since_seq=retained[0].dropped_since_seq + merged_count,
            )
        output.extend(retained)
    return tuple(output)


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


class HarnessActorRegistration(Protocol):
    """Handle owning one harness actor's liveliness and inbox endpoint.

    ``healthy`` is the reconciliation lease: it becomes false once this
    handle, or any child route it owns, can no longer serve the actor.
    """

    @property
    def healthy(self) -> bool: ...

    def close(
        self,
        *,
        reason: str = "unspecified",
        initiator: str = "external-caller",
    ) -> None: ...


@dataclass(slots=True)
class _InflightAttempt:
    """One request currently handed to (or waiting for) a worker.

    This is the clock for the no-event path: the deadline runs from the
    moment the request entered the worker's queue, and the *only* thing that
    resets it is a progress observation correlated with this delivery.  A
    process being alive, online, or reconnecting is not progress and does not
    reset it (see ``_publish_harness_progress``).
    """

    identity: AttemptIdentity
    budget_ms: int
    last_progress_ms: int
    reported: bool = False


class DaemonEventBridge:
    """Bridge actor events and own only run-marker/route resource handles."""

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
        workflow_outcome: Callable[[HarnessResult], bool] | None = None,
        forwarder: Forwarder | None = None,
        owner_notifier: Callable[..., object] | None = None,
        owner_requester_addresses: Collection[str] = (),
    ) -> None:
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
        # The quota watchdog hears each PROVIDER_USAGE_LIMIT turn.  It only
        # observes: settlement below is unchanged (Allen 09-17: exhausted
        # agents keep today's handling).
        self._usage_limit_observer = usage_limit_observer
        self._workflow_outcome = workflow_outcome
        self._pending_workflow_results: dict[str, HarnessResult] = {}
        # Answers whose reply did not confirm on their tick, keyed by the
        # delivery they answer.  Retried as the SAME reply (idempotent) and
        # never re-dispatched meanwhile: the answer exists, asking the model
        # again is what looped wangshuo-sprite on 2026-09-24.
        self._pending_reply_results: dict[str, HarnessResult] = {}
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        # One mapping from a supervisor-local short name to the network
        # identity.  Registration and the delivery pump must share it: keys
        # that disagree recreate the queue-forever trap this closes.
        self.harness_actor_uri = harness_actor_uri or (lambda name: name)
        self._marker = _RunMarker(self.state_dir / "daemon-run.json")
        self._mailbox_registration = None
        self._actor_registrations: dict[str, HarnessActorRegistration] = {}
        self._started = False
        self._retry_pump_kick = threading.Event()
        self._retry_pump_halt = threading.Event()
        self._retry_pump_thread: threading.Thread | None = None
        # Sender-facing fail-loud state (2026-09-21).  ``_inflight`` is keyed by
        # delivery id: a delivery belongs to exactly one worker, and the id is
        # what both progress events and harness results carry back.  Nothing
        # here influences selection -- it exists so an unproductive attempt is
        # reported instead of being routed around.
        self._inflight: dict[str, _InflightAttempt] = {}
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
            tuple[InboxMessage, HarnessResult] | None
        ] = queue.Queue()
        self._forward_outcomes: queue.Queue[
            tuple[InboxMessage, HarnessResult, ForwardOutcome]
        ] = queue.Queue()
        self._forward_thread: threading.Thread | None = None

    def start(self) -> DaemonRecoverySummary:
        if self._started:
            raise RuntimeError("daemon runtime is already started")
        state = self.desired_state.load()
        previous_unclean = self._marker.begin()
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

        collect_orphans = getattr(self.harnesses, "collect_orphans", None)
        harness_orphans_retired = (
            _timed("harnesses.collect_orphans", collect_orphans)
            if callable(collect_orphans)
            else 0
        )
        harness_restarts = _timed("harnesses.reconcile", self.harnesses.reconcile)
        _timed("harnesses.sync_session_refs", self._sync_harness_session_refs)
        _timed("harnesses.reconcile_actors", self._reconcile_harness_actors)
        published_progress = _timed(
            "harnesses.publish_progress", self._publish_harness_progress
        )
        completed_results = _timed(
            "harnesses.complete_results", self._complete_harness_results
        )
        harness_deliveries = _timed(
            "harnesses.dispatch_deliveries", self._dispatch_harness_deliveries
        )
        # The no-event loud path rides the same tick: a delivery that has
        # produced no correlated progress within its harness budget is
        # reported here, and notices the inbox did not accept are retried.
        # Runs after dispatch so a delivery that just started its first
        # attempt has its clock registered before it is judged.
        availability_notices = _timed(
            "availability.report_stalled", self._report_stalled_deliveries
        )
        drain_failed = getattr(self.harnesses, "drain_failed_events", None)
        failed = (
            drain_failed() if callable(drain_failed) else ()
        )
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
        inbox_pruned_items = _timed("inbox.prune", self._prune_inbox)
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

    def _kick_retry_pump(self) -> None:
        """Nudge the delivery retry pump without waiting for network I/O.

        ``retry_due`` waits on outbound sends up to the facade's authority
        timeout; an unreachable peer would otherwise charge every reconcile
        tick that full wait and starve the steps queued behind it
        (2026-09-14: 62 of 99 tick overruns were this one call).  The tick
        sets the kick and moves on; the pump thread does the waiting.
        """

        self._retry_pump_kick.set()

    def _retry_pump_run(self) -> None:
        while not self._retry_pump_halt.is_set():
            self._retry_pump_kick.wait(_RETRY_PUMP_IDLE_SECONDS)
            self._retry_pump_kick.clear()
            if self._retry_pump_halt.is_set():
                return
            started_at = time.monotonic()
            try:
                results = self.inbox.retry_due()
            except Exception as error:  # noqa: BLE001 - pump isolation boundary
                if _stale_fence_rejection(error):
                    # Lost a fence race to the tick thread: yield this round
                    # at info, not error -- the pump and the tick are meant
                    # to run concurrently now, and a stale round per kick
                    # would read as a persistent fault it is not.
                    if self._logger is not None:
                        self._logger(
                            "info",
                            "daemon",
                            "inbox.retry_pump.yielded",
                            detail=str(error)[:200],
                            durationMs=int((time.monotonic() - started_at) * 1000),
                        )
                    continue
                # The pump is the only thing allowed to block on delivery;
                # its failures are logged and retried on the next kick, never
                # propagated into the reconcile tick -- and never allowed to
                # kill the pump itself silently (the lifecycle-thread shape).
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "inbox.retry_pump.failed",
                        errorType=type(error).__name__,
                        detail=str(error)[:500],
                        durationMs=int((time.monotonic() - started_at) * 1000),
                    )
                continue
            duration_ms = int((time.monotonic() - started_at) * 1000)
            if self._logger is not None and (
                results or duration_ms >= _RETRY_PUMP_SLOW_MS
            ):
                self._logger(
                    "info",
                    "daemon",
                    "inbox.retry_pump.completed",
                    inboxResults=len(results),
                    durationMs=duration_ms,
                )

    def _halt_retry_pump(self) -> None:
        thread = self._retry_pump_thread
        self._retry_pump_thread = None
        if thread is None:
            return
        self._retry_pump_halt.set()
        self._retry_pump_kick.set()
        thread.join(_RETRY_PUMP_JOIN_SECONDS)
        if thread.is_alive() and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "inbox.retry_pump.stop_timeout",
                detail="retry pump did not leave a blocking retry_due call",
            )

    def _sync_harness_session_refs(self) -> None:
        """Ask the Harness actor to reconcile learned refs into desired state.

        This is what lets a restarted daemon resume worker conversations
        instead of cold-starting every connector.  Persistence is
        best-effort: a state-dir I/O failure must not break the reconcile
        loop (and with it every other duty), so it is logged and retried on
        the next pass.
        """

        try:
            self.harnesses.reconcile_session_refs()
        except (OSError, DesiredStateError) as error:
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "harness.session_ref.persist_failed",
                    detail=str(error),
                )

    def _reconcile_harness_actors(self) -> None:
        """Keep one actor registration per live streaming harness.

        Every agent is a remote to the network: the daemon registers the
        connector's liveliness token and inbox endpoint so deliveries flow
        through the exact same zenoh path whether the recipient runs on this
        host or another.  A connector that dies loses its registration, so
        later sends queue durably instead of vanishing.
        """

        if self.harness_actor_registrar is None:
            return
        active = set(self.harnesses.streaming_actors())
        for name in sorted(active):
            registration = self._actor_registrations.get(name)
            if registration is not None and registration.healthy:
                continue
            if registration is not None:
                # Remove the stale key before closing/redeclaring.  If either
                # operation fails, the next reconcile sees a missing key and
                # retries instead of preserving a permanently deaf entry.
                self._actor_registrations.pop(name)
                registration.close(
                    reason="reconcile-unhealthy",
                    initiator="daemon-runtime",
                )
            self._actor_registrations[name] = self.harness_actor_registrar(
                self.harness_actor_uri(name)
            )
        for name in tuple(self._actor_registrations.keys() - active):
            registration = self._actor_registrations.pop(name)
            registration.close(
                reason="actor-inactive",
                initiator="daemon-runtime",
            )

    def _dispatch_harness_deliveries(self) -> int:
        accepted = 0
        refresh_hold = getattr(self.inbox, "refresh_hold", None)
        can_refresh_hold = callable(refresh_hold)
        dispatchable = getattr(self.inbox, "dispatchable_messages", None)
        for actor in self.harnesses.streaming_actors():
            # The inbox is keyed by the canonical network identity — the
            # exact key the registrar advertised for this connector.
            recipient = self.harness_actor_uri(actor)
            messages = (
                dispatchable(recipient, now_ms=self._clock_ms())
                if callable(dispatchable)
                else self.inbox.pending_messages(recipient)
            )
            for message in messages:
                if message.message_id in self._pending_reply_results:
                    # Already answered; only its reply is still settling.
                    # drain_results() released the worker's dedup, so a
                    # dispatch here would run the whole turn again.
                    continue
                # Start (or keep) the no-progress clock for every request this
                # live worker owes a receipt for, even before it accepts the
                # enqueue: "queued but never picked up" is one of the silent
                # shapes this change exists to report.
                self._note_delivery_seen(actor, message)
                if self.harnesses.dispatch(
                    actor,
                    HarnessDelivery(
                        delivery_id=message.message_id,
                        conversation_id=message.conversation_id,
                        sender=message.sender,
                        recipient=message.recipient,
                        message=self._message_text(message),
                        origin=self._message_origin(message),
                    ),
                ):
                    accepted += 1
                    # #276: a worker just genuinely accepted this delivery
                    # into its queue (StreamingHarnessProcess.enqueue dedups
                    # by delivery id, so `dispatch` only returns True once
                    # per delivery) — in-flight is not "nobody picked this
                    # up", so the hold TTL restarts from here instead of
                    # counting down from receipt while the turn runs.
                    if can_refresh_hold:
                        refresh_hold(message.message_id)
            notice_reader = getattr(self.inbox, "system_notices", None)
            dismiss_notice = getattr(self.inbox, "dismiss_system_notice", None)
            if callable(notice_reader) and callable(dismiss_notice):
                for notice in notice_reader(self.harness_actor_uri(actor)):
                    if self.harnesses.dispatch(
                        actor,
                        HarnessDelivery(
                            delivery_id=notice.message_id,
                            conversation_id=notice.conversation_id,
                            sender=notice.sender,
                            recipient=notice.recipient,
                            message=self._message_text(notice),
                            origin=self._message_origin(notice),
                        ),
                    ):
                        # System notices are offered once.  They have no result,
                        # acknowledgement, receipt, or FIFO settlement duty.
                        dismiss_notice(notice.message_id)
                        accepted += 1
        return accepted

    def _publish_harness_progress(self) -> int:
        """Coalesce one tick of worker progress and offer it to each sender.

        Progress is drained before terminal results on purpose: the original
        request row is still pending here, and that row is the authoritative
        source of the original sender (the route-C recipient).
        """

        submit = getattr(self.inbox, "submit_progress_event", None)
        if not callable(submit):
            return 0
        refresh_hold = getattr(self.inbox, "refresh_hold", None)
        can_refresh_hold = callable(refresh_hold)
        events = tuple(
            event
            for event in self.harnesses.drain_progress()
            if isinstance(event, ProgressEvent)
        )
        if not events:
            return 0
        by_actor: dict[str, dict[str, InboxMessage]] = {}
        published = 0
        for event in _coalesce_progress_events(events):
            originals = by_actor.get(event.actor)
            if originals is None:
                originals = {
                    message.message_id: message
                    for message in self.inbox.pending_messages(event.actor)
                }
                by_actor[event.actor] = originals
            original = originals.get(event.delivery_id)
            if original is None:
                # The request may have been manually acked while the worker
                # kept running.  Progress is advisory; never invent a route.
                continue
            # Concrete progress signal for the fail-loud clock.  This is the
            # same correlated worker activity that slides the dispatch hold
            # (#276), and it is the single observable that resets a delivery's
            # no-progress budget.  A live pid, "online", or a reconnect
            # attempt is NOT progress and must not reset it.
            self._note_delivery_progress(event.delivery_id, self._clock_ms())
            if can_refresh_hold:
                # #276: correlated worker activity mid-turn is the same
                # "still being worked" signal dispatch acceptance is, so it
                # slides the hold deadline the same way — a long but active
                # turn keeps its inbox row alive tick over tick; a worker
                # that stops emitting progress gets no further refreshes and
                # the row still expires (and still prunes) on schedule.
                refresh_hold(original.message_id)
            if submit(event, recipient=original.sender):
                published += 1
        return published

    def _complete_harness_results(self) -> int:
        settled = 0
        # Forwards the pump finished since the last tick settle first, here,
        # under the same settlement owner as every other harness result.
        while True:
            try:
                original, result, outcome = self._forward_outcomes.get_nowait()
            except queue.Empty:
                break
            if outcome.accepted:
                self._finish_attempt(result.delivery_id)
                if self.inbox.ack(original.recipient, original.message_id).acknowledged:
                    settled += 1
                continue
            failed = replace(
                result,
                status=HarnessResultStatus.FAILED,
                output="",
                error=outcome.error or outcome.failure_code,
                failure_code=outcome.failure_code or "HARNESS_TRANSIENT_FAILURE",
            )
            if self._settle_failed_result(failed, original):
                settled += 1
        results = [
            *self._pending_workflow_results.values(),
            *self._pending_reply_results.values(),
            *self.harnesses.drain_results(),
        ]
        for result in results:
            # Re-added below only if its reply fails to confirm again.
            self._pending_reply_results.pop(result.delivery_id, None)
            if self._workflow_outcome is not None:
                try:
                    handled = self._workflow_outcome(result)
                    if handled:
                        self.inbox.ack(result.recipient, result.delivery_id)
                        self._finish_attempt(result.delivery_id)
                        self._pending_workflow_results.pop(result.delivery_id, None)
                        settled += 1
                        continue
                    self._pending_workflow_results.pop(result.delivery_id, None)
                except (NameError, ImportError):
                    raise
                except Exception as error:
                    self._pending_workflow_results[result.delivery_id] = result
                    if self._logger:
                        self._logger("warn", "pac", "workflow.outcome_deferred", messageId=result.delivery_id, detail=str(error))
                    continue
            from hyprial.pac.delivery_guard import WITHDRAWN, delivery_current
            if result.failure_code == WITHDRAWN and not delivery_current(self.state_dir, result.delivery_id, now_ms=self._clock_ms()):
                self.inbox.ack(result.recipient, result.delivery_id)
                self._finish_attempt(result.delivery_id)
                settled += 1
                continue
            if result.status is HarnessResultStatus.INTERRUPTED:
                continue
            original = next(
                (
                    message
                    for message in self.inbox.pending_messages(result.recipient)
                    if message.message_id == result.delivery_id
                ),
                None,
            )
            if original is None:
                # The row can be fetched/consumed (or already carry a terminal
                # settlement) before this failure lands.  That used to be a
                # silent drop; the authoritative sender is still durable in
                # the settlement tombstone, so look it up and report instead.
                if result.status is HarnessResultStatus.FAILED:
                    fallback = self._failure_original(result.delivery_id)
                    if fallback is not None:
                        self._loud_harness_failure(result, fallback)
                        self._finish_attempt(result.delivery_id)
                        continue
                    self._log_missing_failure_route(result)
                continue
            if result.status is HarnessResultStatus.FAILED:
                if self._settle_failed_result(result, original):
                    settled += 1
                continue
            if result.forward_to is not None:
                # Before the reply-intent short-circuit below: a person
                # answering in-thread arrives as a reply, and acking it
                # without sending would drop the forward silently.  The
                # attempt stays open until the send settles.
                if self._forwarder is None:
                    if self._settle_failed_result(
                        replace(
                            result,
                            status=HarnessResultStatus.FAILED,
                            error="this daemon cannot forward",
                            failure_code=FORWARD_UNAVAILABLE,
                        ),
                        original,
                    ):
                        settled += 1
                    continue
                self._start_forward_thread()
                self._forward_jobs.put((original, result))
                continue
            self._finish_attempt(result.delivery_id)
            try:
                if original.intent == "reply":
                    acknowledged = self.inbox.ack(
                        original.recipient, original.message_id
                    ).acknowledged
                else:
                    acknowledged = self._reply_and_ack(original, result)
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                # Settled per result: raising here used to abort the whole
                # tick after drain_results() had already released every
                # result in the batch, so the rest of the batch was lost.
                acknowledged = (
                    self.inbox.ack(original.recipient, original.message_id).acknowledged
                    if self._reply_already_settled(original, result, error)
                    else False
                )
            if acknowledged:
                settled += 1
        return settled

    def _reply_already_settled(
        self,
        original: InboxMessage,
        result: HarnessResult,
        error: InboxAuthorityTimeout | InboxAuthorityUnavailable,
    ) -> bool:
        """Classify an answer whose reply or ack did not confirm on this tick.

        True: the delivery is already durably answered -- the reply id is
        derived from the delivery, so a receipt conflict means a reply
        committed (after its submit timed out) and a re-run turn produced
        different text; the caller acks it, nothing is answered again.
        False: any other authority error; the answer is kept and the SAME
        reply is retried next tick.
        """

        if _reply_already_answered(error):
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "harness.reply.already_answered",
                    messageId=original.message_id,
                    recipient=original.recipient,
                )
            return True
        first_deferral = result.delivery_id not in self._pending_reply_results
        self._pending_reply_results[result.delivery_id] = result
        if first_deferral and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.reply.deferred",
                messageId=original.message_id,
                recipient=original.recipient,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )
        return False

    def _settle_failed_result(
        self, result: HarnessResult, original: InboxMessage
    ) -> bool:
        """Settle one FAILED turn against its still-pending inbox row."""

        failure_code = result.failure_code or classify_harness_failure(
            result.error
        )
        try:
            failure = self.inbox.settle_harness_failure(
                original.recipient,
                original.message_id,
                failure_code,
                permanent=harness_failure_is_permanent(failure_code),
                max_attempts=HARNESS_FAILURE_MAX_ATTEMPTS,
                backoff_ms=HARNESS_FAILURE_BACKOFF_MS,
                now_ms=self._clock_ms(),
            )
        except KeyError:
            # A concurrent public ack can retire the row after the
            # read above.  That is a completed ownership decision,
            # not a reason to crash/restart the inbox authority.
            return False
        if self._logger is not None:
            self._logger(
                "error" if failure.terminal else "warn",
                "daemon",
                (
                    "harness.delivery.terminal_failed"
                    if failure.terminal
                    else "harness.delivery.retry_scheduled"
                ),
                messageId=failure.message_id,
                recipient=failure.recipient,
                failureCode=failure.failure_code,
                attempts=failure.attempts,
                maxAttempts=failure.max_attempts,
                permanent=failure.permanent,
                terminal=failure.terminal,
                **(
                    {"terminalReason": failure.terminal_reason}
                    if failure.terminal_reason is not None
                    else {"nextAttemptMs": failure.next_attempt_ms}
                ),
            )
        if (
            failure.failure_code == "PROVIDER_USAGE_LIMIT"
            and self._usage_limit_observer is not None
        ):
            try:
                self._usage_limit_observer(failure.recipient)
            except Exception as error:  # noqa: BLE001 - an observer never breaks settlement
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "quota_watchdog.observe_failed",
                        recipient=failure.recipient,
                        error=type(error).__name__,
                    )
        # The sender that is waiting for this receipt is told with
        # this attempt's own evidence.  The owner's quota watchdog
        # above is a separate, retained channel and does not stand in
        # for this one (the 2026-09-21 incident: the owner was told
        # within a second and the sender was told nothing).
        self._loud_harness_failure(result, original, failure=failure)
        self._finish_attempt(result.delivery_id)
        return True

    # ------------------------------------------------------------- forwards

    def _start_forward_thread(self) -> None:
        if self._forward_thread is not None:
            return
        self._forward_thread = threading.Thread(
            target=self._run_forwards, name="hyprial-forward-pump", daemon=True
        )
        self._forward_thread.start()

    def _run_forwards(self) -> None:
        forwarder = self._forwarder
        assert forwarder is not None
        while True:
            job = self._forward_jobs.get()
            if job is None:
                return
            original, result = job
            assert result.forward_to is not None
            try:
                outcome = forwarder(original, result.forward_to, result.output)
            except Exception as error:  # noqa: BLE001 - a send fault is a failed forward, never a dead pump
                outcome = ForwardOutcome(
                    False,
                    "HARNESS_TRANSIENT_FAILURE",
                    f"forward raised {type(error).__name__}",
                )
            self._forward_outcomes.put((original, result, outcome))

    def _halt_forward_thread(self) -> None:
        thread = self._forward_thread
        self._forward_thread = None
        if thread is None:
            return
        self._forward_jobs.put(None)
        thread.join(timeout=5.0)

    # ------------------------------------------------ availability fail-loud

    def _harness_kind(self, actor: str) -> str | None:
        """Connector kind for a supervised worker, used only for its budget."""

        try:
            state = self.desired_state.load()
        except Exception:  # noqa: BLE001 - a reporting path never breaks a tick
            return None
        for spec in state.harnesses:
            if spec.name == actor:
                return spec.harness
        return None

    def _is_user_proxy(self, recipient: str) -> bool:
        """True when ``recipient`` is a local user-proxy, i.e. a person's chat."""

        parsed = parse_agent_uri(recipient)
        if parsed is None or parsed[1] != self.node_id:
            return False
        return self._harness_kind(parsed[2]) == "user-proxy"

    def _note_delivery_seen(self, actor: str, message: InboxMessage) -> None:
        """Start the no-progress clock for a request a live worker owes.

        Registered when the delivery is first offered, not only when the
        worker accepts it: a connector that never gets ready is one of the
        silent shapes this reports.  Re-seeing a delivery is a no-op so the
        clock is not restarted tick over tick.
        """

        if message.message_id in self._inflight:
            return
        if _notice_kind(message) is not None:
            return  # nobody waits on a notice; reporting it would chain
        harness = self._harness_kind(actor)
        now = self._clock_ms()
        # The clock starts when this daemon hands the request over (or finds it
        # already queued for a live worker).  Deliberately not the request's
        # ``created_at_ms``: measuring from inbox arrival would fire a burst of
        # reports for every historical row on daemon start, and a late report
        # is the safer error than a spurious one.  A restart therefore grants
        # an in-flight attempt a fresh budget.
        started = now
        generation = self._attempt_generation.get(message.message_id, 0) + 1
        self._inflight[message.message_id] = _InflightAttempt(
            identity=AttemptIdentity(
                delivery_id=message.message_id,
                conversation_id=message.conversation_id,
                sender=message.sender,
                worker=self.harness_actor_uri(actor),
                harness=harness,
                generation=generation,
                observed_at_ms=now,
            ),
            budget_ms=no_progress_budget_seconds(harness) * 1_000,
            last_progress_ms=started,
        )

    def _note_delivery_progress(self, delivery_id: str, now_ms: int) -> None:
        attempt = self._inflight.get(delivery_id)
        if attempt is not None:
            attempt.last_progress_ms = now_ms

    def _finish_attempt(self, delivery_id: str) -> None:
        attempt = self._inflight.pop(delivery_id, None)
        if attempt is not None:
            # Remember the generation so a retry of the same delivery is a
            # new attempt for the once-per-notice key, not a replay.
            self._attempt_generation[delivery_id] = attempt.identity.generation

    def _attempt_was_acknowledged(self, delivery_id: str) -> bool:
        """Whether durable inbox state says the worker finished this attempt.

        Acknowledgement is authoritative, rather than reply submission: a
        native reply can still be queued or fail settlement, while ACK is the
        durable boundary that the inbound delivery was consumed.  Native
        reply acknowledges that delivery after its reply settles, and an
        explicit ACK reaches the same boundary.
        """

        reader = getattr(self.inbox, "is_acknowledged", None)
        if not callable(reader):
            return False
        try:
            return bool(reader(delivery_id))
        except Exception:  # noqa: BLE001 - an unavailable read is not an ACK
            return False

    def _failure_original(self, delivery_id: str) -> InboxMessage | None:
        """Authoritative sender of a delivery whose row is no longer pending."""

        reader = getattr(self.inbox, "harness_failure_original", None)
        if not callable(reader):
            return None
        try:
            return reader(delivery_id)
        except Exception:  # noqa: BLE001 - lookup failure must not hide the report
            return None

    def _settlement(self, delivery_id: str) -> object | None:
        reader = getattr(self.inbox, "harness_failure_settlement", None)
        if not callable(reader):
            return None
        try:
            return reader(delivery_id)
        except Exception:  # noqa: BLE001
            return None

    def _log_missing_failure_route(self, result: HarnessResult) -> None:
        if self._logger is None:
            return
        self._logger(
            "error",
            "daemon",
            "availability_loud.route_missing",
            deliveryId=result.delivery_id,
            recipient=result.recipient,
            failureCode=result.failure_code
            or classify_harness_failure(result.error),
            detail="no pending row and no settlement tombstone for the sender",
        )

    def _unavailable_notice_for(
        self,
        original: InboxMessage,
        *,
        recipient: str,
        failure_code: str,
        settlement: object | None,
        observed_at_ms: int,
    ) -> InboxMessage:
        """Build the sender notice from durable facts when possible.

        The attempt number comes from the *settlement* (the durable record of
        how many times this delivery failed), not from in-process state, so
        the deterministic message id is a pure function of facts that survive
        a restart.  That is what lets ``_recover_owed_notices`` re-derive the
        exact same notice after a crash instead of inventing a second one.
        """

        attempt = self._inflight.get(original.message_id)
        durable_attempts = int(getattr(settlement, "attempts", 0) or 0)
        if durable_attempts > 0:
            generation = durable_attempts
        elif attempt is not None:
            generation = attempt.identity.generation
        else:
            generation = max(
                1, self._attempt_generation.get(original.message_id, 0) + 1
            )
        identity = AttemptIdentity(
            delivery_id=original.message_id,
            conversation_id=original.conversation_id,
            sender=original.sender,
            worker=(attempt.identity.worker if attempt is not None else recipient),
            harness=attempt.identity.harness if attempt is not None else None,
            generation=generation,
            observed_at_ms=observed_at_ms,
        )
        return unavailable_notice(
            identity,
            failure_code=failure_code,
            terminal=bool(getattr(settlement, "terminal", True)),
            attempts=int(getattr(settlement, "attempts", generation)),
            max_attempts=int(
                getattr(settlement, "max_attempts", HARNESS_FAILURE_MAX_ATTEMPTS)
            ),
            # The classifier's code is the evidence; the raw provider text is
            # not copied here (it can carry credential material).
            detail=None,
        )

    def _loud_harness_failure(
        self,
        result: HarnessResult,
        original: InboxMessage,
        *,
        failure: object | None = None,
    ) -> InboxMessage | None:
        """Tell the waiting sender that *this* attempt failed, with evidence.

        The owner channel (quota watchdog, provider-auth alerts) is a separate
        retained path; this is the sender's own copy and is never satisfied by
        it.  The message is a failure report: it does not acknowledge or settle
        the original delivery and does not pretend to be the result.
        """

        if _notice_kind(original) is not None:
            # A notice about a notice is the chain that flooded allen-proxy
            # (2026-09-26): its failure is logged, never reported onward.
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "availability_loud.notice_of_notice_suppressed",
                    deliveryId=result.delivery_id,
                    recipient=original.sender,
                    failureCode=result.failure_code
                    or classify_harness_failure(result.error),
                )
            return None
        failure_code = result.failure_code or classify_harness_failure(result.error)
        settlement = failure if failure is not None else self._settlement(result.delivery_id)
        message = self._unavailable_notice_for(
            original,
            recipient=result.recipient,
            failure_code=failure_code,
            settlement=settlement,
            observed_at_ms=self._clock_ms(),
        )
        self._submit_loud(message)
        return message

    def _notice_already_durable(self, message_id: str) -> bool:
        """True when the notice is already delivered and acknowledged.

        Delivery without acknowledgement is deliberately not treated as done:
        re-submitting the same deterministic id is idempotent at the inbox, so
        re-sending is the safe side of that ambiguity.
        """

        reader = getattr(self.inbox, "is_acknowledged", None)
        if not callable(reader):
            return False
        try:
            return bool(reader(message_id))
        except Exception:  # noqa: BLE001 - an unknown id is simply not durable
            return False

    def _recover_owed_notices(self, since_ms: int | None) -> int:
        """Re-derive sender notices the previous (unclean) run may have owed.

        A notice that could not be *submitted* before a crash lived only in
        this process's ``_pending_notices``, which is exactly the "silence
        replaced by another silence" shape this unit exists to remove.  The
        durable half -- the terminal settlement row -- still exists, so the
        restart reads it and re-emits the same deterministic notice.  Events
        that only *schedule* a retry need no recovery here: their inbox row is
        still pending, so the ordinary dispatch path re-derives them.
        """

        reader = getattr(self.inbox, "terminal_failure_settlements", None)
        if not callable(reader) or since_ms is None:
            return 0
        try:
            settlements = reader(since_ms=since_ms)
        except Exception as error:  # noqa: BLE001 - recovery is best-effort, but loud
            if self._logger is not None:
                self._logger(
                    "error",
                    "daemon",
                    "availability_loud.recovery_failed",
                    errorType=type(error).__name__,
                    sinceMs=since_ms,
                )
            return 0
        recovered = 0
        for settlement in settlements:
            original = self._failure_original(settlement.message_id)
            if original is None:
                continue
            message = self._unavailable_notice_for(
                original,
                recipient=settlement.recipient,
                failure_code=settlement.failure_code,
                settlement=settlement,
                observed_at_ms=self._clock_ms(),
            )
            if self._notice_already_durable(message.message_id):
                continue
            if self._submit_loud(message):
                recovered += 1
        if self._logger is not None and settlements:
            self._logger(
                "warn",
                "daemon",
                "availability_loud.recovery",
                candidates=len(settlements),
                recovered=recovered,
                sinceMs=since_ms,
            )
        return recovered

    def _flush_pending_notices_on_stop(self) -> None:
        """Last chance for held notices, and a loud record of any that remain.

        On a clean shutdown there is no next start to recover from, so an
        undeliverable notice must at least say so -- the identifiers go to the
        log rather than disappearing with the process.
        """

        try:
            self._retry_pending_notices()
        except Exception:  # noqa: BLE001 - shutdown must not be masked
            pass
        if not self._pending_notices or self._logger is None:
            return
        for message in self._pending_notices.values():
            self._logger(
                "error",
                "daemon",
                "availability_loud.delivery_abandoned",
                messageId=message.message_id,
                recipient=message.recipient,
                idempotencyKey=message.idempotency_key,
                detail="shutdown with the notice still undelivered",
            )

    def _submit_loud(self, message: InboxMessage) -> bool:
        """Submit one notice; hold it for retry if the inbox refuses it.

        A notice that was not accepted is never reported as delivered: it is
        kept (so it is not lost) and retried on a later tick.
        """

        if is_human_facing_requester(message.recipient) or self._is_user_proxy(
            message.recipient
        ):
            # Never into a person's chat.  A user-proxy is one: it relays
            # everything it receives into its person's DM (2026-09-26: 252
            # notices to allen-proxy in a self-feeding chain).
            #
            # Handing it to the owner is the fallback, not the rule: when the
            # waiting sender IS the owner, "the owner" is the same person and
            # the wrap only adds a layer to text they were never meant to
            # receive (2026-09-26: the owner asked why the same notice kept
            # arriving about his own requests).  Those are logged, not re-handed.
            # Not held for retry; diversion happens only here, once per notice.
            self._pending_notices.pop(message.message_id, None)
            requester_is_owner = self._request_reaches_the_owner(message)
            redirected = self._owner_notifier is not None and not requester_is_owner
            if self._logger is not None:
                if requester_is_owner:
                    detail = (
                        "human-facing requester is the owner; notice logged, "
                        "not sent to the owner"
                    )
                elif redirected:
                    detail = (
                        "human-facing requester; notice sent to the owner, "
                        "not to the requester"
                    )
                else:
                    detail = "human-facing requester; notice logged, not sent"
                self._logger(
                    "warn",
                    "daemon",
                    "availability_loud.diverted",
                    messageId=message.message_id,
                    recipient=message.recipient,
                    idempotencyKey=message.idempotency_key,
                    notification=_notice_kind(message),
                    requesterIsOwner=requester_is_owner,
                    redirectedToOwner=redirected,
                    detail=detail,
                )
            if redirected:
                self._notify_owner_of_diverted(message)
            return False
        submit = getattr(self.inbox, "submit", None)
        if not callable(submit):
            self._remember_pending_notice(message, code="INBOX_SUBMIT_UNAVAILABLE")
            return False
        try:
            result = submit(message)
        except Exception as error:  # noqa: BLE001 - the tick owns this boundary
            self._remember_pending_notice(message, code=type(error).__name__)
            return False
        if not result.accepted:
            self._remember_pending_notice(message, code=result.code or "SUBMIT_REJECTED")
            return False
        self._pending_notices.pop(message.message_id, None)
        self._notice_failures_logged.discard(message.message_id)
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "availability_loud.sent",
                messageId=result.message_id,
                recipient=message.recipient,
                idempotencyKey=message.idempotency_key,
            )
        return True

    def _request_reaches_the_owner(self, message: InboxMessage) -> bool:
        """True when re-handing this notice would reach the waiting sender.

        The addresses come from the composition root, which is the only place
        that knows which adapter, routes and ``user:`` address belong to this
        owner.  Conversation ids count too: a notice for a request made inside
        the owner's own chat reaches them whichever address proposed it.
        """

        if not self._owner_requester_addresses:
            return False
        return (
            message.recipient in self._owner_requester_addresses
            or message.conversation_id in self._owner_requester_addresses
        )

    def _notify_owner_of_diverted(self, message: InboxMessage) -> None:
        """Hand a diverted notice to the owner channel on its own thread."""

        notifier = self._owner_notifier
        assert notifier is not None
        text = (
            f"一条原本要发给 {message.recipient} 的失败/无进展提醒，因对方是真人会话"
            f"（{message.conversation_id}）而未发送，改发给你：\n"
            f"{_owner_facing_notice_text(message)}"
        )
        key = f"owner-diverted:{message.idempotency_key or message.message_id}"

        def deliver() -> None:
            try:
                notifier(text, idempotency_key=key)
            except Exception as error:  # noqa: BLE001 -- never into the loop
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "availability_loud.owner_notify_failed",
                        messageId=message.message_id,
                        recipient=message.recipient,
                        detail=str(error) or type(error).__name__,
                    )

        threading.Thread(
            target=deliver, name="hyprial-owner-diverted-notice", daemon=True
        ).start()

    def _remember_pending_notice(self, message: InboxMessage, *, code: str) -> None:
        self._pending_notices.setdefault(message.message_id, message)
        if message.message_id in self._notice_failures_logged:
            return
        self._notice_failures_logged.add(message.message_id)
        if self._logger is not None:
            self._logger(
                "error",
                "daemon",
                "availability_loud.delivery_failed",
                messageId=message.message_id,
                recipient=message.recipient,
                code=code,
                detail="notice held for retry; not delivered",
            )

    def _retry_pending_notices(self) -> int:
        delivered = 0
        for message in tuple(self._pending_notices.values()):
            if self._submit_loud(message):
                delivered += 1
        return delivered

    def _report_stalled_deliveries(self) -> int:
        """Report attempts whose budget elapsed with no correlated progress.

        Clock-driven, not event-driven: the stuck class has no failure event
        to wait for.  The message says "no observable progress, cause not yet
        determined" -- it never claims a permission/approval cause, and it
        never kills or interrupts the turn.  One report per attempt.
        """

        for attempt in tuple(self._inflight.values()):
            if self._attempt_was_acknowledged(attempt.identity.delivery_id):
                self._finish_attempt(attempt.identity.delivery_id)
        self._retry_pending_notices()
        now = self._clock_ms()
        reported = 0
        for attempt in tuple(self._inflight.values()):
            if attempt.reported:
                continue
            waited_ms = now - attempt.last_progress_ms
            if waited_ms < attempt.budget_ms:
                continue
            identity = replace(attempt.identity, observed_at_ms=now)
            message = no_progress_notice(
                identity, waited_ms=waited_ms, budget_ms=attempt.budget_ms
            )
            if self._submit_loud(message):
                reported += 1
            attempt.reported = True
        return reported

    def _reply_and_ack(self, original: InboxMessage, result: HarnessResult) -> bool:
        reply = InboxMessage(
            message_id=str(
                uuid5(NAMESPACE_URL, f"hyprial-agent-sdk-reply:{original.message_id}")
            ),
            conversation_id=original.conversation_id,
            sender=original.recipient,
            recipient=original.sender,
            payload=json.dumps(
                {"message": result.output}, separators=(",", ":")
            ).encode(),
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            idempotency_key=f"reply:{original.message_id}",
            created_at_ms=time.time_ns() // 1_000_000,
        )
        submitted = self.inbox.submit(reply)
        if not submitted.accepted:
            return False
        return self.inbox.ack(original.recipient, original.message_id).acknowledged

    @staticmethod
    def _message_origin(message: InboxMessage) -> dict[str, Any] | None:
        try:
            body = json.loads(message.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        origin = body.get("origin") if isinstance(body, dict) else None
        return origin if isinstance(origin, dict) and origin else None

    @staticmethod
    def _message_text(message: InboxMessage) -> str:
        try:
            body = json.loads(message.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return message.payload.decode("utf-8", errors="replace")
        if isinstance(body, dict):
            value = body.get("message")
            if isinstance(value, str) and value:
                return value
        return message.payload.decode("utf-8", errors="replace")

    def stop(self) -> None:
        if not self._started:
            return
        # Give held notices their last in-process chance before the port closes,
        # and make any that still cannot go out explicit in the log.
        self._flush_pending_notices_on_stop()
        # Halt the retry pump before owned resources close, so its blocking
        # facade call cannot race teardown.
        self._halt_retry_pump()
        self._halt_forward_thread()
        errors = self._close_owned_resources()
        try:
            self._marker.finish()
        except OSError as error:
            errors.append(error)
        finally:
            self._started = False
        if errors:
            raise ExceptionGroup("daemon shutdown failed", errors)

    def _close_owned_resources(self) -> list[Exception]:
        """Close only route registrations and the run marker owned here.

        Domain actors, inbox custody and transport lifetime belong to the
        application composition root and are drained there in dependency
        order.  This bridge must never stop them as a hidden second owner.
        """

        errors: list[Exception] = []
        operations: list[Callable[[], Any]] = []
        for name in tuple(self._actor_registrations):
            registration = self._actor_registrations.pop(name)
            operations.append(
                lambda registration=registration: registration.close(
                    reason="daemon-stop",
                    initiator="daemon-runtime",
                )
            )
        if self._mailbox_registration is not None:
            registration = self._mailbox_registration
            self._mailbox_registration = None
            operations.append(registration.close)
        for operation in operations:
            try:
                operation()
            except (OSError, RuntimeError) as error:
                errors.append(error)
        return errors


def _notice_kind(message: InboxMessage) -> str | None:
    """The ``notification`` kind of a fail-loud notice, for its log line."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return None
    kind = body.get("notification") if isinstance(body, dict) else None
    return kind if isinstance(kind, str) else None



def _notice_text(message: InboxMessage) -> str:
    """The human-readable ``message`` of a fail-loud notice (else the raw body)."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return str(message.payload)
    text = body.get("message") if isinstance(body, dict) else None
    return text if isinstance(text, str) else json.dumps(body, ensure_ascii=False)


#: Lines of a fail-loud notice that only an operator reads.  The owner is told
#: who was waiting and what happened -- the worker URI, the silence budget and
#: the attempt id stay in the daemon log, where an operator goes for them.
_OWNER_HIDDEN_NOTICE_LABELS = ("- worker:", "- 静默预算:", "- attempt:")


def _owner_facing_notice_text(message: InboxMessage) -> str:
    """The notice text with its operator-only fields dropped."""

    return "\n".join(
        line
        for line in _notice_text(message).splitlines()
        if not line.startswith(_OWNER_HIDDEN_NOTICE_LABELS)
    )
