"""DaemonEventBridge reconcile cluster: retry pump, harness actor reconciliation, dispatch deliveries, holds and progress.

Production wiring composes the bridge with actor_mode=True and the headless
cell delivery port (cell model I5-2 C).  Scan, capture, claim, offer and
settlement all stay here: the one addition before an offer is a cell
snapshot read — the port returns the admitted envelope or None, and None
means "no offer this round" (the row stays pending, nothing is fetched,
refused or acknowledged).  The snapshot read that produced an envelope is
the linearization point of that work decision; the envelope's stamp rides
with the in-flight attempt so a later steer reuses the original member
instead of re-reading the cell.  Per-worker dispatch still gets a bounded
offer deadline: an agent that does not settle is skipped for that round,
logged once, and left pending for the next round instead of holding the
tick or its peers.
"""

from __future__ import annotations

from dataclasses import replace

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
from hyprial.daemon.impl.inbox.contracts.origin import OriginClaimant
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.daemon.impl.api  import (
    HarnessDelivery,
)
from hyprial.kernel import DesiredStateError
from hyprial.daemon.impl.inbox.batch.render import (
    render_scalar_notice,
    render_scalar_summary,
)
from hyprial.daemon.impl.inbox.batch.service import capture_batch
from hyprial.daemon.impl.inbox.batch.spill import SpillUnavailable
from hyprial.daemon.impl.dispatch.runtime.offers import DispatchAgentTimeout
from hyprial.daemon.impl.harnesses.turn_delivery.protocol import (
    is_mechanism_hook_request,
    mechanism_of,
)
from hyprial.kernel import AdmissionResult
if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.contracts import CellDispatchEnvelope

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
        stale_streak = 0
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
                    stale_streak += 1
                    if stale_streak == 20 and self._logger is not None:
                        self._logger(
                            "warn", "daemon", "inbox.retry_pump.starved",
                            consecutiveYields=stale_streak,
                        )
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
                stale_streak = 0
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
            stale_streak = 0
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
        self._retry_origin_claim_releases()
        self._reconcile_batch_claims(active, generations)
        active_workers = {self.harness_actor_uri(name) for name in active}
        for attempt in tuple(self._inflight.values()):
            if attempt.identity.worker not in active_workers:
                self._finish_attempt(attempt.identity.delivery_id)
        if self.harness_actor_registrar is None:
            return
        for name in sorted(active):
            try:
                self._reconcile_active_actor(name, generations.get(name))
            except Exception as error:  # noqa: BLE001 - per-actor tick isolation
                self._log_actor_failure(name, "registration", error)
        for name in tuple(self._actor_registrations.keys() - active):
            registration = self._actor_registrations.pop(name)
            self._actor_registration_generations.pop(name, None)
            try:
                registration.close(reason="actor-inactive", initiator="daemon-runtime")
            except Exception as error:  # noqa: BLE001 - per-actor close isolation
                self._log_actor_failure(name, "registration.close", error)

    def _reconcile_active_actor(self, name: str, generation: int | None) -> None:
        registration = self._actor_registrations.get(name)
        if (
            registration is not None
            and generation is not None
            and self._actor_registration_generations.get(name) != generation
        ):
            # Close the old process token now; declare its replacement on a
            # later tick so the network observes an offline gap.
            self._actor_registrations.pop(name)
            self._actor_registration_generations.pop(name, None)
            registration.close(reason="actor-inactive", initiator="daemon-runtime")
            return
        if registration is not None and registration.healthy:
            return
        if registration is not None:
            # Remove before closing so failure cannot preserve a deaf entry.
            self._actor_registrations.pop(name)
            self._actor_registration_generations.pop(name, None)
            registration.close(reason="reconcile-unhealthy", initiator="daemon-runtime")
        self._actor_registrations[name] = self.harness_actor_registrar(
            self.harness_actor_uri(name)
        )
        if generation is not None:
            self._actor_registration_generations[name] = generation

    def _injection_message_text(self, message: InboxMessage) -> str:
        """What a model carrier injects (WP5-A §3.5).

        Hook exchanges and python_worker frames keep the raw event/body; every
        model context gets the summary-only block, so a forged hook marker on
        an ordinary request cannot smuggle the body in.
        """

        harness = self._recipient_harness_kind(message.recipient)
        if harness is not None and (
            is_mechanism_hook_request(message) or mechanism_of(harness) == "python_worker"
        ):
            return self._message_text(message)
        return render_scalar_summary(
            message_id=message.message_id,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            summary=message.summary or "",
            body_bytes=len(message.payload),
        )

    def _dispatch_harness_deliveries(self) -> int:
        self._retry_batch_fetches()
        accepted = self._drain_dispatch_offer_outcomes()
        pending_result_ids = getattr(
            self.harnesses, "pending_result_delivery_ids", lambda: frozenset()
        )()
        pending_result_ids = pending_result_ids.union(
            member
            for _, members in self._pending_batch_fetches.values()
            for member in members
        )
        for actor in self.harnesses.streaming_actors():
            try:
                accepted += self._dispatch_actor_deliveries(actor, pending_result_ids)
            except DispatchAgentTimeout as timeout:
                self._log_agent_dispatch_timeout(
                    actor, timeout.elapsed_ms, timeout.operation_id
                )
            except Exception as error:  # noqa: BLE001 - per-actor tick isolation
                self._log_actor_failure(actor, "dispatch", error)
        return accepted

    def _log_agent_dispatch_timeout(
        self, actor: str, elapsed_ms: int, operation_id: str
    ) -> None:
        announced = getattr(self, "_dispatch_timeout_announced", None)
        if announced is None:
            announced = self._dispatch_timeout_announced = set()
        if operation_id in announced:
            return
        announced.add(operation_id)
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "harness.dispatch.agent_timeout",
                agent=actor,
                durationMs=elapsed_ms,
                code="DISPATCH_AGENT_TIMEOUT",
            )

    def _log_actor_failure(self, actor: str, phase: str, error: Exception) -> None:
        if self._logger is not None:
            self._logger(
                "error", "daemon", "harness.actor.failed",
                actor=actor, phase=phase, errorType=type(error).__name__,
            )

    def _recipient_harness_kind(self, recipient: str) -> str | None:
        """The worker kind behind an inbox row's recipient key.

        A name URI's last segment is the worker name.  An ``a.`` id URI names
        the worker whose network identity it is, so that worker answers
        instead of the id itself (``_recipient_worker_name``).
        """

        name = self._recipient_worker_name(recipient)
        return self._delivery_harness_kind(name) if name is not None else None

    def _delivery_harness_kind(self, actor: str) -> str | None:
        from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES

        kind = self._harness_kind(actor)
        if kind not in DECLARED_HARNESSES:
            warned = getattr(self, "_unknown_harness_actors", None)
            if warned is None:
                warned = self._unknown_harness_actors = set()
            if actor not in warned:
                warned.add(actor)
                if self._logger is not None:
                    self._logger("warn", "daemon", "harness.kind.unknown",
                                 actor=actor, harness=kind)
            return None
        return kind

    def _admit_delivery(
        self, actor: str, delivery: HarnessDelivery
    ) -> "CellDispatchEnvelope | None":
        """One cell snapshot read: the admitted envelope, or None to defer."""

        return self._cell_delivery.headless_envelope(actor, delivery)

    def _submit_envelope(
        self,
        actor: str,
        envelope: "CellDispatchEnvelope",
        *,
        original: InboxMessage,
        retry: bool,
        notice: bool,
    ) -> bool:
        """Offer one admitted envelope to this worker's execution lane."""

        offers = self._dispatch_offers
        if offers is None:
            dispatch = getattr(self.harnesses, "dispatch", None)
            return callable(dispatch) and bool(dispatch(envelope))
        if getattr(self, "_actor_mode", False):
            return offers.offer(
                actor, envelope, original=original, retry=retry, notice=notice
            )
        return offers.offer_sync(
            actor,
            envelope,
            original=original,
            retry=retry,
            notice=notice,
            timeout_ms=getattr(self, "_dispatch_agent_timeout_ms", 1500),
        )

    def _offer_delivery(
        self,
        actor: str,
        delivery: HarnessDelivery,
        *,
        original: InboxMessage,
        retry: bool,
        notice: bool,
    ) -> "CellDispatchEnvelope | None":
        """Admit one delivery through the cell port and offer its envelope.

        Returns the admitted envelope when the offer was submitted for
        execution, and None when the cell deferred this round, no execution
        seam exists, or the submission itself was refused or timed out —
        every shape leaves the row pending with nothing fetched (M′ M2).
        Rounds 3-4: a FIRST attempt starts its in-flight clock at cell
        admission, before the submission, so a busy lane, a refused or
        timed-out submission and a later execution-side refusal all keep
        the clock exactly as the pre-cell runtime did; a row the cell
        defers never starts one.  A retry keeps the pre-cell distinction:
        its prior terminal result already finished the old clock, so its
        new generation is registered only after the submission is actually
        accepted — a worker that still holds the delivery (enqueue dedup)
        must not start a second clock.  A seen-again row keeps its clock
        and adopts the newest stamp, so a later steer targets the member
        of the most recent submitted envelope.
        """

        if not notice and not self._claim_scalar_origin(actor, original):
            return None
        envelope = self._admit_delivery(actor, delivery)
        if envelope is None:
            self._release_scalar_origin(original.message_id, notice=notice)
            return None
        if not retry:
            self._note_delivery_seen(actor, original, stamp=envelope.stamp)
        if not self._submit_claimed_envelope(
            actor, envelope, original=original, retry=retry, notice=notice
        ):
            return None
        if retry:
            self._note_delivery_seen(actor, original, stamp=envelope.stamp)
        return envelope

    def _dispatch_actor_deliveries(self, actor: str, pending_result_ids: frozenset[str]) -> int:
        """Dispatch one actor inside the caller's fault boundary."""
        accepted = 0
        admission_reader = getattr(self.inbox, "admitted_dispatchable", None)
        notice_reader = getattr(self.inbox, "system_notices", None)
        dismiss_notice = getattr(self.inbox, "dismiss_system_notice", None)
        actor_held = False
        # The inbox is keyed by the canonical network identity — the
        # exact key the registrar advertised for this connector.
        recipient = self.harness_actor_uri(actor)
        keys = self._actor_read_keys(actor)
        if self._blocked_actor is not None and self._blocked_actor(recipient):
            return accepted
        pending_batches = getattr(self, "_pending_batch_claims", None)
        batch_active = bool(
            (pending_batches is not None and actor in pending_batches)
            or self._batch_claims.get(actor)
        )
        offers = self._dispatch_offers
        if offers is not None:
            announced = getattr(self, "_dispatch_timeout_announced", None)
            if announced is None:
                announced = self._dispatch_timeout_announced = set()
            active = offers.active(actor)
            if active is None:
                announced.intersection_update(offers.active_operation_ids())
            else:
                elapsed_ms = offers.active_elapsed_ms(actor)
                timeout_ms = getattr(self, "_dispatch_agent_timeout_ms", 1500)
                if elapsed_ms >= timeout_ms:
                    self._log_agent_dispatch_timeout(
                        actor, elapsed_ms, active.operation_id
                    )
                return accepted
        pending_messages = self._read_across(keys, self.inbox.pending_messages)
        # The link gate runs on the inbox actor thread (review B3); the
        # runtime only names rows it has already answered or is settling so
        # those never newly occupy W.
        exclude = frozenset(
            message.message_id
            for message in pending_messages
            if message.message_id in pending_result_ids
            or message.message_id in self._pending_settle_results
        )
        messages = (
            self._read_across(
                keys,
                lambda key: self._admitted_dispatchable_for(key, recipient, exclude),
            )
            if callable(admission_reader)
            else pending_messages
        )
        notices = (
            tuple(notice_reader(recipient))
            if callable(notice_reader) and callable(dismiss_notice)
            else ()
        )
        if self._delivery_hooks is not None:
            present = {message.message_id for message in (*pending_messages, *notices)}
            for key in keys:
                self._delivery_hooks.forget_missing_deliveries(key, present)
        # One daemon-rendered batch per idle actor, plus the retained
        # scalar path for mechanism hook requests (never model text).
        batch_members, batch_dispatched = self._dispatch_batch(
            actor,
            recipient,
            messages=messages,
            pending_result_ids=pending_result_ids,
        )
        if batch_dispatched:
            accepted += 1
        for message in messages:
            if message.message_id in batch_members:
                # Already claimed by the accepted batch delivery.
                continue
            hook_request = is_mechanism_hook_request(message)
            if batch_active and not hook_request:
                continue
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
            if message.message_id in self._pending_settle_results:
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
            delivery = HarnessDelivery(
                delivery_id=message.message_id,
                conversation_id=message.conversation_id,
                sender=message.sender,
                recipient=message.recipient,
                message=self._injection_message_text(message),
                origin=self._stored_message_origin(message),
                hook_request=hook_request,
                workflow=self._is_workflow_message(message),
            )
            if self._delivery_hooks is not None:
                if not delivery.hook_request:
                    prepared = self._delivery_hooks.prepare_delivery(delivery)
                    if prepared is None:
                        # Inbox order is the actor's delivery order.  A
                        # bounded hook may hold the head, but no younger
                        # ordinary message may pass it during this tick.
                        # Hook requests are the one mechanism exception.
                        actor_held = True
                        continue
                    delivery = prepared
            envelope = self._offer_delivery(
                actor, delivery, original=message,
                retry=is_retry, notice=False,
            )
            if envelope is not None:
                if getattr(self, "_actor_mode", False):
                    break
                if self._delivery_hooks is not None:
                    self._delivery_hooks.mark_dispatched(message.message_id)
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
        if actor_held or batch_active:
            return accepted
        if callable(notice_reader) and callable(dismiss_notice):
            for notice in notices:
                delivery = HarnessDelivery(
                    delivery_id=notice.message_id,
                    conversation_id=notice.conversation_id,
                    sender=notice.sender,
                    recipient=notice.recipient,
                    message=render_scalar_notice(
                        sender=notice.sender,
                        recipient=notice.recipient,
                        text=notice.summary or "",
                        created_at_ms=notice.created_at_ms,
                    ),
                    origin=self._stored_message_origin(notice),
                    hook_request=is_mechanism_hook_request(notice),
                    notice=True,
                )
                if self._delivery_hooks is not None:
                    if not delivery.hook_request:
                        prepared = self._delivery_hooks.prepare_delivery(delivery)
                        if prepared is None:
                            break
                        delivery = prepared
                if self._offer_delivery(
                    actor, delivery, original=notice,
                    retry=False, notice=True,
                ):
                    if getattr(self, "_actor_mode", False):
                        break
                    if self._delivery_hooks is not None:
                        self._delivery_hooks.mark_dispatched(notice.message_id)
                        self._delivery_hooks.forget_delivery(notice.message_id)
                    # System notices are offered once.  They have no result,
                    # acknowledgement, receipt, or FIFO settlement duty.
                    dismiss_notice(notice.message_id)
                    accepted += 1
        return accepted

    def _is_workflow_message(self, message: InboxMessage) -> bool:
        from hyprial.daemon.impl.pac.contracts.delivery import is_workflow_delivery

        return is_workflow_delivery(self.state_dir, message.message_id)

    def _model_eligible(self, message: InboxMessage) -> bool:
        """May this row join a model-facing batch?

        Mechanism hook requests and Python/TypeSafe worker protocol frames stay
        scalar and isolated by contract: their payload is machine protocol, not
        text to concatenate with a model batch.
        """

        if is_mechanism_hook_request(message):
            return False
        harness = self._recipient_harness_kind(message.recipient)
        # Unknown kinds use scalar summaries, not full-body model batches.
        return harness is not None and mechanism_of(harness) != "python_worker"

    def _dispatch_batch(
        self,
        actor: str,
        recipient: str,
        *,
        messages: tuple[InboxMessage, ...],
        pending_result_ids: frozenset[str],
    ) -> tuple[tuple[str, ...], bool]:
        """Offer one already-rendered batch to this actor's managed worker.

        A managed batch claims every member before enqueue so the next tick
        cannot also start scalar turns for younger rows.  The turn result is
        telemetry: no member is replied to or acked from it.
        """

        claimed = self._batch_claims.get(actor, ())
        pending_batches = getattr(self, "_pending_batch_claims", None)
        if pending_batches is not None and actor in pending_batches:
            return claimed, False
        candidates = tuple(
            message
            for message in messages
            if message.message_id not in pending_result_ids
            and message.message_id not in self._pending_settle_results
            and self._model_eligible(message)
            and message.message_id not in claimed
        )
        if not candidates or claimed:
            # One accepted, unconsumed batch edge per actor: no successor while
            # a batch turn still owns its members.
            return claimed, False
        # The caller's per-actor boundary logs failures and retries next tick;
        # do not turn a failed capture into an unlogged scalar fallback.
        if not callable(getattr(self.inbox, "batch_snapshot", None)):
            # Minimal inbox ports retain their scalar-summary delivery path.
            # A supported batch port that raises still fails this actor only.
            return claimed, False
        home_reader = getattr(self, "_batch_actor_home", None)
        actor_home = home_reader(recipient) if callable(home_reader) else None
        try:
            view = capture_batch(
                self.inbox,
                recipient,
                keys=self._actor_read_keys(actor),
                limit_bytes=self._inbox_batch_inline_bytes(),
                actor_home=actor_home,
                epoch="",
                include_notices=False,
                only=tuple(message.message_id for message in candidates),
                mark_fetched=False,
                origin_claim=OriginClaimant.headless(recipient),
            )
        except SpillUnavailable as error:
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "harness.batch.spill_fallback",
                    agent=recipient,
                    errorType=type(error).__name__,
                )
            return claimed, False
        if not view.text or not view.cursor:
            return claimed, False
        members = view.message_ids
        if not members:
            return claimed, False
        claimed_candidates = self._prepare_batch_origin_claim(
            recipient,
            members,
            getattr(view, "origin_claim_seq", None),
            candidates,
        )
        if not claimed_candidates:
            return claimed, False
        first = claimed_candidates[0]
        delivery = HarnessDelivery(
            # A batch is only a one-shot transport wrapper, never an identity.
            # The queue's required key is therefore the first real member ID;
            # ``message_ids`` remains the complete ordered settlement set.
            delivery_id=members[0],
            conversation_id=first.conversation_id,
            sender=first.sender,
            recipient=first.recipient,
            message=view.text,
            origin=self._stored_message_origin(first),
            batch_cursor=view.cursor,
            message_ids=members,
            workflow=any(
                self._is_workflow_message(message) for message in claimed_candidates
            ),
        )
        # Preserve the existing first-member hook interception.  The batch
        # already carries that member's ID, so no wrapper identity is minted.
        if self._delivery_hooks is not None:
            hook_delivery = replace(
                delivery, delivery_id=first.message_id, message_ids=()
            )
            prepared = self._delivery_hooks.prepare_delivery(hook_delivery)
            if prepared is None:
                self._release_origin_claims(members)
                return claimed, False
            delivery = replace(delivery, hook_text=prepared.hook_text)
        envelope = self._admit_delivery(actor, delivery)
        if envelope is None:
            self._release_origin_claims(members)
            return claimed, False
        pending_batches = getattr(self, "_pending_batch_claims", None)
        if pending_batches is None:
            pending_batches = self._pending_batch_claims = {}
        pending_batches[actor] = (members, claimed_candidates, envelope.stamp)
        self._begin_batch_claim(actor, members)
        self._publish_batch_claims()
        try:
            offered = self._submit_envelope(
                actor, envelope, original=first, retry=False, notice=False
            )
        except DispatchAgentTimeout:
            if getattr(self, "_dispatch_offers", None) is None:
                pending_batches.pop(actor, None)
                self._release_batch_claim(members[0], members, reason="offer-timeout")
            raise
        if not offered:
            self._release_batch_claim(members[0], members, reason="offer-refused")
            return claimed, False
        pending_batches.pop(actor, None)
        self._claim_batch(
            actor,
            members,
            claimed_candidates,
            mark_fetched=not getattr(self, "_actor_mode", False),
            cell_stamp=envelope.stamp,
        )
        return self._batch_claims.get(actor, claimed), True

    def _inbox_batch_inline_bytes(self) -> int:
        value = getattr(self, "_inbox_batch_inline_limit_bytes", None)
        return value if isinstance(value, int) and not isinstance(value, bool) else 16 * 1024

    def _drain_dispatch_offer_outcomes(self) -> int:
        offers = self._dispatch_offers
        if offers is None:
            return 0
        accepted = 0
        pending_batches = getattr(self, "_pending_batch_claims", None)
        for outcome in offers.completed():
            late_batch = (
                pending_batches.get(outcome.worker)
                if pending_batches is not None
                else None
            )
            if outcome.error is not None and self._logger is not None:
                self._logger(
                    "warn", "daemon", "dispatch.offer_failed",
                    deliveryId=outcome.delivery_id,
                    worker=outcome.worker,
                    detail=outcome.error,
                )
            if not outcome.accepted:
                if outcome.message_ids:
                    self._release_batch_claim(
                        outcome.delivery_id, outcome.message_ids, reason="offer-refused"
                    )
                elif not outcome.notice:
                    self._release_origin_claims((outcome.delivery_id,))
                continue
            if late_batch is not None:
                pending_batches.pop(outcome.worker, None)
                late_members, late_messages, late_stamp = late_batch
                self._claim_batch(
                    outcome.worker,
                    late_members,
                    late_messages,
                    mark_fetched=True,
                    cell_stamp=late_stamp,
                )
            elif self._batch_claims.get(outcome.worker) and outcome.message_ids:
                self._confirm_batch_fetched(
                    outcome.recipient, outcome.message_ids
                )
            if self._delivery_hooks is not None:
                self._delivery_hooks.mark_dispatched(outcome.delivery_id)
            if outcome.notice:
                dismiss_notice = getattr(self.inbox, "dismiss_system_notice", None)
                if callable(dismiss_notice):
                    dismiss_notice(outcome.delivery_id)
                if self._delivery_hooks is not None:
                    self._delivery_hooks.forget_delivery(outcome.delivery_id)
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
