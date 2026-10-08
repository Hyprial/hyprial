"""DaemonEventBridge forward/delivery cluster: forward thread, delivery tracking, loud failure notices and owner notification."""

from __future__ import annotations
from hyprial.daemon.impl.runtime.settlement import (
    HARNESS_FAILURE_MAX_ATTEMPTS,
    ForwardOutcome,
    HarnessActorRegistration,
    _InflightAttempt,
    _notice_kind,
    _owner_facing_notice_text,
)
import json
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable
from uuid import NAMESPACE_URL, uuid5
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.kernel import parse_agent_uri
from hyprial.daemon import (
    AttemptIdentity,
    is_human_facing_requester,
    no_progress_budget_seconds,
    no_progress_notice,
    unavailable_notice,
)
from hyprial.daemon.impl.api  import (
    HarnessResult,
    classify_harness_failure,
)
if TYPE_CHECKING:
    pass

class _BridgeForwardsMixin:
    """DaemonEventBridge cluster; the composing class owns the state."""

    def _forget_turn_hook_delivery(self, delivery_id: str) -> None:
        if self._turn_hooks is not None:
            self._turn_hooks.forget_delivery(delivery_id)

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
            original, result, attempt = job
            assert result.forward_to is not None
            try:
                outcome = forwarder(original, result.forward_to, result.output)
            except Exception as error:  # noqa: BLE001 - a send fault is a failed forward, never a dead pump
                outcome = ForwardOutcome(
                    False,
                    "HARNESS_TRANSIENT_FAILURE",
                    f"forward raised {type(error).__name__}",
                )
            self._forward_outcomes.put((original, result, outcome, attempt))

    def _halt_forward_thread(self) -> None:
        thread = self._forward_thread
        self._forward_thread = None
        if thread is None:
            return
        self._forward_jobs.put(None)
        thread.join(timeout=5.0)

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
        if message.intent == "reply" or _notice_kind(message) is not None:
            return  # nobody waits on a reply or notice; reporting it would chain
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
        entity_token: str | None = None
        identity = self._blocking_failure_identity
        if self._blocking_failure_observer is not None and identity is not None:
            try:
                entity_token = identity(message.recipient)
            except Exception as error:  # noqa: BLE001 - dispatch remains available
                if self._logger is not None:
                    self._logger(
                        "error",
                        "daemon",
                        "agent.block.identity_failed",
                        recipient=message.recipient,
                        error=type(error).__name__,
                    )
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
            agent_entity_token=entity_token,
            budget_ms=no_progress_budget_seconds(harness) * 1_000,
            last_progress_ms=started,
        )

    def _note_delivery_progress(self, delivery_id: str, now_ms: int) -> None:
        attempt = self._inflight.get(delivery_id)
        if attempt is not None:
            self._inflight[delivery_id] = replace(
                attempt, last_progress_ms=now_ms
            )

    def _finish_attempt(self, delivery_id: str) -> _InflightAttempt | None:
        attempt = self._inflight.pop(delivery_id, None)
        if attempt is not None:
            # Remember the generation so a retry of the same delivery is a
            # new attempt for the once-per-notice key, not a replay.
            self._attempt_generation[delivery_id] = attempt.identity.generation
        return attempt

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
            failureCode=result.failure_code or classify_harness_failure(result.error),
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
        attempt: _InflightAttempt | None = None,
    ) -> InboxMessage:
        """Build the sender notice from durable facts when possible.

        The attempt number comes from the *settlement* (the durable record of
        how many times this delivery failed), not from in-process state, so
        the deterministic message id is a pure function of facts that survive
        a restart.  That is what lets ``_recover_owed_notices`` re-derive the
        exact same notice after a crash instead of inventing a second one.
        """

        if attempt is None:
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
        attempt: _InflightAttempt | None = None,
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
        settlement = (
            failure if failure is not None else self._settlement(result.delivery_id)
        )
        message = self._unavailable_notice_for(
            original,
            recipient=result.recipient,
            failure_code=failure_code,
            settlement=settlement,
            observed_at_ms=self._clock_ms(),
            attempt=attempt,
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
            self._remember_pending_notice(
                message, code=result.code or "SUBMIT_REJECTED"
            )
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
            self._inflight[attempt.identity.delivery_id] = replace(
                attempt, reported=True
            )
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
                {
                    "message": result.output,
                    "origin": {
                        "via": "daemon",
                        "onBehalfOf": original.recipient,
                    },
                },
                separators=(",", ":"),
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

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            if not self._startup_in_progress:
                return
            self._started = True
        # A failed stop, including a normal run's failed registration close,
        # retains its exact owner references and may be retried.
        self._startup_in_progress = True
        # No notice effect may still be mutating retry custody when the final
        # shutdown flush reads it.
        if self._availability_lane is not None:
            if not self._availability_lane.close(5.0):
                raise RuntimeError("availability notice effects did not drain")
            self._availability_lane = None
        # Give held notices their last in-process chance before the port closes,
        # and make any that still cannot go out explicit in the log.
        self._flush_pending_notices_on_stop()
        for lane in (
            self._harness_reconcile_lane, self._session_ref_lane, self._prune_lane,
            self._result_claim_lane,
        ):
            if lane is not None and not lane.close(5.0):
                raise RuntimeError("Harness maintenance effect did not drain")
        self._harness_reconcile_lane = None
        self._session_ref_lane = None
        self._prune_lane = None
        self._result_claim_lane = None
        if self._result_settlement is not None:
            if not self._result_settlement.close(5.0):
                raise RuntimeError("Harness result settlements did not drain")
            self._result_settlement = None
        if self._forward_settlement is not None:
            if not self._forward_settlement.close(5.0):
                raise RuntimeError("accepted Harness forwards did not drain")
            self._forward_settlement = None
        if self._dispatch_offers is not None:
            offers = self._dispatch_offers
            if not offers.drain(5.0):
                raise RuntimeError("accepted Harness dispatch offers did not drain")
            self._drain_dispatch_offer_outcomes()
            if not offers.close(5.0):
                raise RuntimeError("Harness dispatch offer owner did not stop")
            self._dispatch_offers = None
        if self._hold_refresh is not None:
            if not self._hold_refresh.close(5.0):
                raise RuntimeError("accepted inbox hold refreshes did not drain")
            self._hold_refresh = None
        if self._progress_publish is not None:
            if not self._progress_publish.close(5.0):
                raise RuntimeError("accepted progress publications did not drain")
            self._progress_publish = None
        # Halt the retry pump before owned resources close, so its blocking
        # facade call cannot race teardown.
        self._halt_retry_pump()
        self._halt_forward_thread()
        if not self._drain_blocking_failures_on_stop(timeout):
            raise RuntimeError(
                "accepted blocking failure observations did not transfer"
            )
        errors = self._close_owned_resources()
        try:
            self._marker.finish()
        except OSError as error:
            errors.append(error)
        finally:
            self._started = False
            if not errors:
                try:
                    self._queued_delivery_holds.clear()
                except (OSError, RuntimeError) as error:
                    errors.append(error)
            if not errors and self._dispatch_state is not None:
                owner = self._dispatch_state
                if not owner.close(5.0):
                    errors.append(RuntimeError("dispatch state actor did not drain"))
                else:
                    self._dispatch_state = None
        if errors:
            raise ExceptionGroup("daemon shutdown failed", errors)
        self._startup_in_progress = False

    def _drain_blocking_failures_on_stop(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._blocking_failure_lock:
                if not self._pending_blocking_failures:
                    return True
            self._retry_blocking_failures()
            with self._blocking_failure_lock:
                if not self._pending_blocking_failures:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _close_owned_resources(self) -> list[Exception]:
        """Close only route registrations and the run marker owned here.

        Domain actors, inbox custody and transport lifetime belong to the
        application composition root and are drained there in dependency
        order.  This bridge must never stop them as a hidden second owner.
        """

        errors: list[Exception] = []
        operations: list[Callable[[], Any]] = []
        for name, registration in tuple(self._actor_registrations.items()):
            def close_actor_registration(
                *, name: str = name, registration: HarnessActorRegistration = registration
            ) -> None:
                registration.close(
                    reason="daemon-stop", initiator="daemon-runtime"
                )
                if self._actor_registrations.get(name) is registration:
                    self._actor_registrations.pop(name, None)

            operations.append(close_actor_registration)
        if self._mailbox_registration is not None:
            registration = self._mailbox_registration

            def close_mailbox_registration() -> None:
                registration.close()
                if self._mailbox_registration is registration:
                    self._mailbox_registration = None

            operations.append(close_mailbox_registration)
        for operation in operations:
            try:
                operation()
            except (OSError, RuntimeError) as error:
                errors.append(error)
        return errors
