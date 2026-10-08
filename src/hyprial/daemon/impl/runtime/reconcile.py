"""DaemonEventBridge reconcile cluster: retry pump, harness actor reconciliation, dispatch deliveries, holds and progress."""

from __future__ import annotations

from hyprial.daemon.impl.runtime.settlement import (
    _RETRY_PUMP_JOIN_SECONDS,
    _RETRY_PUMP_RETRY_FLOOR_SECONDS,
    _RETRY_PUMP_SLOW_MS,
    _retry_pump_wait_seconds,
    _QueuedDeliveryHold,
    _coalesce_progress_events,
    _stale_fence_rejection,
)
import time
from typing import TYPE_CHECKING
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.daemon.impl.api  import (
    HarnessDelivery,
)
from hyprial.kernel import DesiredStateError
from hyprial.daemon.impl.harnesses.turn_delivery.protocol  import is_hook_request
from hyprial.kernel import AdmissionResult
if TYPE_CHECKING:
    pass

class _BridgeReconcileMixin:
    """DaemonEventBridge cluster; the composing class owns the state."""

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
        floor_next_round = False
        while not self._retry_pump_halt.is_set():
            wait_seconds = _retry_pump_wait_seconds(
                self.inbox,
                now_ms=self._clock_ms(),
            )
            if floor_next_round:
                wait_seconds = max(
                    wait_seconds,
                    _RETRY_PUMP_RETRY_FLOOR_SECONDS,
                )
            timed_out = not self._retry_pump_kick.wait(wait_seconds)
            self._retry_pump_kick.clear()
            if self._retry_pump_halt.is_set():
                return
            if floor_next_round and not timed_out:
                # An empty/failed round owns a real backoff. Generic kicks
                # may invalidate ordinary due-aware waits, not this floor.
                continue
            due_reader = getattr(self.inbox, "next_retry_due_ms", None)
            if (
                not timed_out
                and callable(due_reader)
                and _retry_pump_wait_seconds(
                    self.inbox,
                    now_ms=self._clock_ms(),
                )
                > 0
            ):
                # Generic reconcile and online events both use the existing
                # kick.  An early signal only invalidates the previous wait;
                # it does not turn a future/empty scheduler into an I/O poll.
                continue
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
                    floor_next_round = True
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
                floor_next_round = True
                continue
            floor_next_round = not results
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

        active = set(self.harnesses.streaming_actors())
        generation_reader = getattr(self.harnesses, "streaming_generations", None)
        generations = generation_reader() if callable(generation_reader) else {}
        active_workers = {self.harness_actor_uri(name) for name in active}
        for attempt in tuple(self._inflight.values()):
            if attempt.identity.worker not in active_workers:
                self._finish_attempt(attempt.identity.delivery_id)
        if self.harness_actor_registrar is None:
            return
        for name in sorted(active):
            registration = self._actor_registrations.get(name)
            generation = generations.get(name)
            if (
                registration is not None
                and generation is not None
                and self._actor_registration_generations.get(name) != generation
            ):
                # The original token belongs to a different process. Close
                # it now and declare the replacement only on a later tick so
                # the network observes an offline gap even for a fast restart.
                self._actor_registrations.pop(name)
                self._actor_registration_generations.pop(name, None)
                registration.close(
                    reason="actor-inactive", initiator="daemon-runtime"
                )
                continue
            if registration is not None and registration.healthy:
                continue
            if registration is not None:
                # Remove the stale key before closing/redeclaring.  If either
                # operation fails, the next reconcile sees a missing key and
                # retries instead of preserving a permanently deaf entry.
                self._actor_registrations.pop(name)
                self._actor_registration_generations.pop(name, None)
                registration.close(
                    reason="reconcile-unhealthy",
                    initiator="daemon-runtime",
                )
            self._actor_registrations[name] = self.harness_actor_registrar(
                self.harness_actor_uri(name)
            )
            if generation is not None:
                self._actor_registration_generations[name] = generation
        for name in tuple(self._actor_registrations.keys() - active):
            registration = self._actor_registrations.pop(name)
            self._actor_registration_generations.pop(name, None)
            registration.close(
                reason="actor-inactive",
                initiator="daemon-runtime",
            )

    def _dispatch_harness_deliveries(self) -> int:
        accepted = self._drain_dispatch_offer_outcomes()
        pending_result_ids = getattr(
            self.harnesses, "pending_result_delivery_ids", lambda: frozenset()
        )()
        dispatchable = getattr(self.inbox, "dispatchable_messages", None)
        notice_reader = getattr(self.inbox, "system_notices", None)
        dismiss_notice = getattr(self.inbox, "dismiss_system_notice", None)
        for actor in self.harnesses.streaming_actors():
            actor_held = False
            # The inbox is keyed by the canonical network identity — the
            # exact key the registrar advertised for this connector.
            recipient = self.harness_actor_uri(actor)
            if self._blocked_actor is not None and self._blocked_actor(recipient):
                continue
            pending_messages = self.inbox.pending_messages(recipient)
            messages = (
                dispatchable(recipient, now_ms=self._clock_ms())
                if callable(dispatchable)
                else pending_messages
            )
            notices = (
                tuple(notice_reader(recipient))
                if callable(notice_reader) and callable(dismiss_notice)
                else ()
            )
            if self._turn_hooks is not None:
                self._turn_hooks.forget_missing_deliveries(
                    recipient,
                    {message.message_id for message in (*pending_messages, *notices)},
                )
            for message in messages:
                hook_request = is_hook_request(message)
                if actor_held and not hook_request:
                    # Keep scanning only for mechanism-owned hook requests.
                    # They must be able to reach a dedicated handler even
                    # when an older ordinary row is waiting on its own hook.
                    continue
                if message.message_id in pending_result_ids:
                    # The Harness authority owns a native result claim or an
                    # accepted enqueue, even if TurnRuntime already released
                    # its own admission during drain_results().
                    continue
                if message.message_id in self._pending_reply_results:
                    # Already answered; only its reply is still settling.
                    # drain_results() released the worker's dedup, so a
                    # dispatch here would run the whole turn again.
                    continue
                # The first attempt starts when a live worker is first offered
                # the delivery, even if enqueue never succeeds: "queued but
                # never picked up" is one of the silent shapes this reports.
                # A retry is different: its prior terminal result finished the
                # old clock, so its new generation starts only after enqueue
                # actually accepts it.
                is_retry = message.message_id in self._attempt_generation
                if not is_retry:
                    self._note_delivery_seen(actor, message)
                delivery = HarnessDelivery(
                    delivery_id=message.message_id,
                    conversation_id=message.conversation_id,
                    sender=message.sender,
                    recipient=message.recipient,
                    message=self._message_text(message),
                    origin=self._message_origin(message),
                    hook_request=hook_request,
                )
                if self._turn_hooks is not None:
                    if not delivery.hook_request:
                        prepared = self._turn_hooks.prepare_delivery(delivery)
                        if prepared is None:
                            # Inbox order is the actor's delivery order.  A
                            # bounded hook may hold the head, but no younger
                            # ordinary message may pass it during this tick.
                            # Hook requests are the one mechanism exception.
                            actor_held = True
                            continue
                        delivery = prepared
                offers = self._dispatch_offers
                if offers is not None:
                    if offers.offer(
                        actor, delivery, original=message,
                        retry=is_retry, notice=False,
                    ):
                        # One in-flight offer per worker keeps that worker's
                        # ordinary inbox order while other workers continue.
                        break
                    continue
                if self.harnesses.dispatch(actor, delivery):
                    if self._turn_hooks is not None:
                        self._turn_hooks.mark_dispatched(message.message_id)
                    if is_retry:
                        self._note_delivery_seen(actor, message)
                    accepted += 1
                    # #276: a worker just genuinely accepted this delivery
                    # into its queue (StreamingHarnessProcess.enqueue dedups
                    # by delivery id, so `dispatch` only returns True once
                    # per delivery) — in-flight is not "nobody picked this
                    # up", so the hold TTL restarts from here instead of
                    # counting down from receipt while the turn runs.
                    self._refresh_queued_hold(
                        message.message_id,
                        worker=recipient,
                        now_ms=self._clock_ms(),
                        force=True,
                    )
            if actor_held:
                continue
            if callable(notice_reader) and callable(dismiss_notice):
                for notice in notices:
                    delivery = HarnessDelivery(
                        delivery_id=notice.message_id,
                        conversation_id=notice.conversation_id,
                        sender=notice.sender,
                        recipient=notice.recipient,
                        message=self._message_text(notice),
                        origin=self._message_origin(notice),
                        hook_request=is_hook_request(notice),
                        notice=True,
                    )
                    if self._turn_hooks is not None:
                        if not delivery.hook_request:
                            prepared = self._turn_hooks.prepare_delivery(delivery)
                            if prepared is None:
                                break
                            delivery = prepared
                    offers = self._dispatch_offers
                    if offers is not None:
                        if offers.offer(
                            actor, delivery, original=notice,
                            retry=False, notice=True,
                        ):
                            break
                        continue
                    if self.harnesses.dispatch(actor, delivery):
                        if self._turn_hooks is not None:
                            self._turn_hooks.mark_dispatched(notice.message_id)
                            self._turn_hooks.forget_delivery(notice.message_id)
                        # System notices are offered once.  They have no result,
                        # acknowledgement, receipt, or FIFO settlement duty.
                        dismiss_notice(notice.message_id)
                        accepted += 1
        return accepted

    def _drain_dispatch_offer_outcomes(self) -> int:
        offers = self._dispatch_offers
        if offers is None:
            return 0
        accepted = 0
        for outcome in offers.completed():
            if outcome.error is not None and self._logger is not None:
                self._logger(
                    "warn", "daemon", "dispatch.offer_failed",
                    deliveryId=outcome.delivery_id,
                    worker=outcome.worker,
                    detail=outcome.error,
                )
            if not outcome.accepted:
                continue
            if self._turn_hooks is not None:
                self._turn_hooks.mark_dispatched(outcome.delivery_id)
            if outcome.notice:
                dismiss_notice = getattr(self.inbox, "dismiss_system_notice", None)
                if callable(dismiss_notice):
                    dismiss_notice(outcome.delivery_id)
                if self._turn_hooks is not None:
                    self._turn_hooks.forget_delivery(outcome.delivery_id)
            else:
                if outcome.retry:
                    self._note_delivery_seen(outcome.worker, outcome.original)
                self._refresh_queued_hold(
                    outcome.delivery_id,
                    worker=outcome.recipient,
                    now_ms=self._clock_ms(),
                    force=True,
                )
            accepted += 1
        return accepted

    def _refresh_queued_hold(
        self,
        message_id: str,
        *,
        worker: str,
        now_ms: int,
        force: bool = False,
    ) -> bool:
        previous = self._queued_delivery_holds.get(message_id)
        if (
            not force
            and previous is not None
            and previous.worker == worker
            and now_ms - previous.refreshed_at_ms < self._hold_refresh_interval_ms
        ):
            return False
        refresh_hold = getattr(self.inbox, "refresh_hold", None)
        if not callable(refresh_hold):
            return False
        hold_owner = self._hold_refresh
        if hold_owner is not None:
            admitted = hold_owner.submit(
                message_id,
                expected=previous,
                replacement=_QueuedDeliveryHold(worker, now_ms),
            )
            if admitted is AdmissionResult.OVERLOADED and self._logger is not None:
                self._logger(
                    "warn", "daemon", "inbox.hold_refresh_overloaded",
                    messageId=message_id,
                )
            return admitted is AdmissionResult.ACCEPTED
        # Keep the established inbox-owned clock boundary.  The runtime clock
        # below controls only throttling; forwarding it into an inbox backed
        # by another clock domain can shorten the durable deadline.
        if not refresh_hold(message_id):
            self._queued_delivery_holds.pop(message_id, None)
            return False
        self._queued_delivery_holds[message_id] = _QueuedDeliveryHold(
            worker=worker,
            refreshed_at_ms=now_ms,
        )
        return True

    def _refresh_live_queued_holds(self) -> int:
        """Keep accepted queue entries alive while their worker stays live."""

        live_workers = {
            self.harness_actor_uri(actor) for actor in self.harnesses.streaming_actors()
        }
        now_ms = self._clock_ms()
        refreshed = 0
        for message_id, hold in tuple(self._queued_delivery_holds.items()):
            if hold.worker not in live_workers:
                self._queued_delivery_holds.pop(message_id, None)
                continue
            if now_ms - hold.refreshed_at_ms < self._hold_refresh_interval_ms:
                continue
            if self._refresh_queued_hold(
                message_id,
                worker=hold.worker,
                now_ms=now_ms,
            ):
                refreshed += 1
        if refreshed and self._logger is not None:
            self._logger(
                "debug",
                "daemon",
                (
                    "inbox.live_holds_refresh_submitted"
                    if self._hold_refresh is not None
                    else "inbox.live_holds_refreshed"
                ),
                count=refreshed,
            )
        return refreshed

    def _publish_harness_progress(self) -> int:
        """Coalesce one tick of worker progress and offer it to each sender.

        Progress is drained before terminal results on purpose: the original
        request row is still pending here, and that row is the authoritative
        source of the original sender (the route-C recipient).
        """

        submit = getattr(self.inbox, "submit_progress_event", None)
        if not callable(submit):
            return 0
        progress_owner = self._progress_publish
        published = progress_owner.take_published() if progress_owner is not None else 0
        events = tuple(
            event
            for event in self.harnesses.drain_progress()
            if isinstance(event, ProgressEvent)
        )
        if not events:
            return published
        by_actor: dict[str, dict[str, InboxMessage]] = {}
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
            now_ms = self._clock_ms()
            self._note_delivery_progress(event.delivery_id, now_ms)
            # Correlated activity slides the same live-worker lease and resets
            # its throttle window. The periodic path also covers accepted work
            # queued behind a turn that emits no progress.
            self._refresh_queued_hold(
                original.message_id,
                worker=event.actor,
                now_ms=now_ms,
            )
            if progress_owner is not None:
                admitted = progress_owner.submit(event, original.sender)
                if admitted is AdmissionResult.OVERLOADED and self._logger is not None:
                    self._logger(
                        "warn", "daemon", "harness.progress_overloaded",
                        deliveryId=event.delivery_id,
                    )
            elif submit(event, recipient=original.sender):
                published += 1
        return published
