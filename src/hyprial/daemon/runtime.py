"""Daemon lifecycle: storage, mailbox role, inbox and managed harnesses."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol
from uuid import NAMESPACE_URL, uuid5

from hyprial.inbox.api import DeliveryLifecycle, InboxMessage, InboxPruneItem
from hyprial.inbox.progress import COALESCE_KEPT_PHASES, ProgressEvent
from hyprial.contracts.readiness import ReadinessReport

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

    @property
    def inbox_pruned(self) -> int:
        """Unconsumed inbox rows evicted by the TTL sweep this tick."""

        return len(self.inbox_pruned_items)


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

    def begin(self) -> bool:
        previous_unclean = self.path.exists()
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
    ) -> None:
        self.state_dir = Path(state_dir)
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
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        # One mapping from a supervisor-local short name to the network
        # identity.  Registration and the delivery pump must share it: keys
        # that disagree recreate the queue-forever trap this closes.
        self.harness_actor_uri = harness_actor_uri or (lambda name: name)
        self._marker = _RunMarker(self.state_dir / "daemon-run.json")
        self._mailbox_registration = None
        self._actor_registrations: dict[str, HarnessActorRegistration] = {}
        self._started = False

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
        self._reconcile_harness_actors()
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
        inbox_results = len(_timed("inbox.retry_due", self.inbox.retry_due))
        # Inbox TTL sweep rides the same periodic tick as the delivery pump:
        # unconsumed rows past their deadline are evicted here, once per
        # reconcile, instead of a timer of their own.  Items are surfaced so
        # the daemon can log per-message ``inbox.pruned`` trajectory events.
        inbox_pruned_items = _timed("inbox.prune", self.inbox.prune_inbox)
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
                if self.harnesses.dispatch(
                    actor,
                    HarnessDelivery(
                        delivery_id=message.message_id,
                        conversation_id=message.conversation_id,
                        sender=message.sender,
                        recipient=message.recipient,
                        message=self._message_text(message),
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
        for result in self.harnesses.drain_results():
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
                continue
            if result.status is HarnessResultStatus.FAILED:
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
                    continue
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
                settled += 1
                continue
            if original.intent == "reply":
                acknowledged = self.inbox.ack(
                    original.recipient, original.message_id
                ).acknowledged
            else:
                acknowledged = self._reply_and_ack(original, result)
            if acknowledged:
                settled += 1
        return settled

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
