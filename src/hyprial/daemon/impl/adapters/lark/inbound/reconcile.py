"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from hyprial.daemon import Alarm

from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    canonical_lark_message_type,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    InboundOutcome,
    LarkInboundMessage,
    ReconcileReport,
)
from hyprial.daemon.impl.adapters.lark.runtime.health import DEFAULT_RECONCILE_LOOKBACK_SECONDS
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.adapters.lark.credentials.scopes import parse_permission_violation
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LARK_HISTORY_CODES_PERMANENT_CHAT_GONE
from hyprial.daemon.impl.adapters.lark.state.records import (
    DeadLetter,
)
# Feishu returns either code when an app without broad group-history access
# attempts to list a chat's messages. Keep this allowlist specific to that one
# endpoint: any other Lark API failure must remain reconciliation-fatal.
_HISTORY_SCOPE_FORBIDDEN_CODES = frozenset({230002, 230027})
DEFAULT_DEAD_LETTER_ALERT_THRESHOLD = 10
DEAD_LETTER_ALERT_WINDOW_SECONDS = 60 * 60
PENDING_SUBMISSION_REPLAY_BATCH = 16
PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS = 5.0
# Platform codes meaning "this chat is not readable, and no retry changes
# that".  They complete the criterion a954c161 introduced -- failures split by
# *whether retrying could ever clear them* -- which until now recognised only
# one way of being permanent, a missing scope.  A chat the bot was removed
# from is equally beyond retry, and landed in the retryable bucket by default.
#
# Documented by the platform (open.feishu.cn, chat and chat-member endpoints):
#   232006  the chat_id is invalid
#   232009  the chat has been dissolved
#   232011  the operator (this bot) is not in the chat
#
# ⚠️ Sourced for the chat endpoints; this sweep calls the message-list
# endpoint, and its own error table was not found in those docs.  That is why
# an unrecognised code still fails closed below rather than being assumed
# permanent: adding a code here may be a no-op, but it can never turn a
# transient failure into a silent pass.
#
# "Permanent" here means "no retry clears it", not "never clears" -- someone
# re-adding the bot fixes 232011, exactly as granting a scope fixes the
# missing-scope case that already sits in this bucket.
_UNREADABLE_CHAT_CODES = frozenset({232006, 232009, 232011})
def _permanent_chat_failure_code(error: BaseException) -> int | None:
    """The platform code when a history refusal is permanent, else None.

    Classification is by SDK error code only -- never by error text, which
    is both unlocalized and credential-bearing.  Anything unrecognized
    (including a ``LarkApiError`` without a code) stays transient so the
    health layer keeps failing closed.
    """

    if (
        isinstance(error, LarkApiError)
        and error.code in LARK_HISTORY_CODES_PERMANENT_CHAT_GONE
    ):
        return error.code
    return None


def _reconcile_error_category(error: BaseException) -> str:
    """Name the failure class from *structured* fields only.

    ``history-permission-unavailable`` is the one incomplete outcome that must
    NOT restart the adapter: a mention-only Feishu app cannot replay messages it
    was never permitted to read, but its websocket subscription still receives
    new @-mentions (worker.py's probe predicate keys off this exact suffix).
    Everything else stays ``history-unavailable`` and keeps failing closed.
    """
    permission_denied = parse_permission_violation(error) is not None
    # Feishu's message-list endpoint can return a history-scope denial without a
    # structured ``permission_violations`` payload.
    history_scope_forbidden = (
        isinstance(error, LarkApiError)
        and error.operation == "list chat messages"
        and error.code in _HISTORY_SCOPE_FORBIDDEN_CODES
    )
    if permission_denied or history_scope_forbidden:
        return "history-permission-unavailable"
    return "history-unavailable"

def _create_time_ms(value: str | None) -> int:
    """Best-effort parse of a millisecond create_time; unparseable sorts old."""

    try:
        return int(value) if value else 0
    except ValueError:
        return 0


class _ReconcileDeadLetterMixin:
    def _notify_sender_of_dead_letter(
        self,
        message: LarkInboundMessage,
        *,
        reason: str,
        code: str | None,
    ) -> None:
        """Best-effort native "not delivered" reply to the sender (F3).

        The dead-letter store is the custody boundary -- the body survives
        for replay and audit; this reply is the sender's only signal that
        their message did not arrive.  It must never break the dead-letter
        path itself, and the per-message idempotency key keeps a replay of
        the same message from re-notifying.
        """
        label = code if code else reason
        text = (
            "Delivery failed\n"
            f"Code: {label}\n"
            "Your message was not delivered. It is preserved in the "
            "dead-letter store for replay."
        )
        try:
            self._lark.reply(
                message.message_id,
                text,
                idempotency_key=f"{message.message_id}:dead-letter:{reason}",
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - notification is best effort
            pass


    def recover_message(self, message_id: str) -> InboundOutcome:
        """Re-drive one native message through the inbound pipeline.

        Uses ``im.v1.message.get`` to pull the original body from the server
        (the endpoint already used for quoted context), so a message lost on
        a dead websocket or a failed forward is recoverable by its native id
        — e.g. one surfaced in a dead letter.  Idempotent: a message that
        already reached Harness custody returns ``duplicate``.
        """

        if self._closing.is_set():
            return InboundOutcome(status="error", ack_error="inbound-closing")
        if self._state.seen_message(message_id):
            # Already delivered; tidy any stale dead letter and stop.
            self._state.clear_dead_letter(message_id)
            return InboundOutcome(status="duplicate")
        pending = self._state.pending_inbound_submission_record(message_id)
        if pending is not None:
            return self._recover_pending_submission(pending)
        message = self._lark.get_inbound_message(message_id)
        if message is None:
            return InboundOutcome(status="not-found")
        return self.handle_inbound(
            self._with_known_chat_type(message), suppress_guidance=True
        )

    def _recover_pending_submission(
        self, pending: tuple[str, str, str], *, deadline: float | None = None
    ) -> InboundOutcome:
        if self._closing.is_set():
            return InboundOutcome(status="error", ack_error="inbound-closing")
        message_id, event_id, frozen = pending
        try:
            envelope = json.loads(frozen)
            request = envelope["request"]
            metadata = request["provider_metadata"]
            if (request["message_id"] != message_id or metadata["messageId"] != message_id
                    or metadata["eventId"] != event_id
                    or not all(isinstance(metadata[key], str) and metadata[key]
                               for key in ("eventId", "chatId"))
                    or not all(isinstance(metadata[key], str)
                               for key in ("chatType", "senderId", "senderType"))):
                raise ValueError("frozen inbound identity mismatch")
            message = LarkInboundMessage(
                event_id=event_id, message_id=message_id, chat_id=metadata["chatId"],
                chat_type=metadata["chatType"], text=request["text"],
                sender_id=metadata["senderId"], sender_type=metadata["senderType"],
                root_id=metadata.get("rootId"), thread_id=metadata.get("threadId"),
                reply_to=metadata.get("replyTo"), create_time=metadata.get("createTime"),
                message_type=metadata.get("messageType") or "text",
            )
        except (KeyError, TypeError, ValueError):
            return InboundOutcome(status="error", ack_error="SubmitOutcomeUnknownError")
        remaining = 30.0 if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return InboundOutcome(status="error", ack_error="recovery-deadline")
        return self._inbound.process(
            message, suppress_guidance=True, timeout=remaining,
            frozen_recovery=True, recovery_deadline=deadline,
        )

    def _unretire_chat(self, chat_id: str) -> None:
        """Lift a chat's retirement on outside-the-sweep membership evidence.

        Caller has already probed that a retirement row exists; the DELETE
        itself stays the conditional write.
        """

        # The DELETE is the side effect; keep it on its own line so no
        # later "tidy" of the condition can reorder it behind a short
        # circuit (a logger-less embedded/test adapter must still lift).
        lifted = self._state.unretire_chat(chat_id)
        if not lifted or self._logger is None:
            return
        try:
            self._logger.log(
                "info",
                "lark.reconcile.chat-unretired",
                adapter=self._state.adapter,
                chat_id=chat_id,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001,S110 - visibility is best effort
            pass

    def _retire_chat(self, chat_id: str, *, code: int) -> None:
        """Exclude a permanently-refused chat from all future sweeps.

        The chat's correlations and dead letters stay as audit records; the
        retirement event carries only the adapter, the chat and the platform
        error code -- never SDK error text (it can contain URLs/tokens).
        """

        newly_retired = self._state.retire_chat(
            chat_id, code=code, now=self._utcnow()
        )
        if not newly_retired or self._logger is None:
            return
        try:
            self._logger.log(
                "warn",
                "lark.reconcile.chat-retired",
                adapter=self._state.adapter,
                chat_id=chat_id,
                code=code,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001,S110 - visibility is best effort
            pass

    def reconcile_recent(
        self,
        *,
        lookback_seconds: int = DEFAULT_RECONCILE_LOOKBACK_SECONDS,
        now_ms: int | None = None,
    ) -> ReconcileReport:
        """Replay messages a websocket outage dropped, per chat, idempotently.

        Scans chats with prior inbound activity (correlations + dead letters)
        for messages inside the lookback window and feeds anything unseen
        through the normal inbound path.  Dedup by native message id makes
        repeat sweeps safe; guidance replies are suppressed so a still-
        unaddressed message is not re-answered on every reconnect.
        """

        if self._closing.is_set():
            return ReconcileReport(0, 0, 0, 0, 0, retryable_errors=("adapter: closing",))
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        window_start_ms = now_ms - lookback_seconds * 1000
        start_time = str(window_start_ms // 1000)
        errors: list[str] = []
        blocked: list[str] = []
        retired: list[str] = []
        scanned = forwarded = duplicates = dead_lettered = 0
        pending_attempted = pending_held = 0
        attempted_ids: set[str] = set()
        deadline = time.monotonic() + PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS
        for pending in self._state.pending_inbound_submission_page(limit=PENDING_SUBMISSION_REPLAY_BATCH):
            if self._closing.is_set() or time.monotonic() >= deadline:
                break
            # Advance only attempted items, before their bounded call. A slow
            # first item cannot monopolize the next sweep, even after restart.
            self._state.advance_pending_inbound_cursor(pending[0])
            attempted_ids.add(pending[0])
            pending_attempted += 1
            scanned += 1
            outcome = self._recover_pending_submission(pending, deadline=deadline)
            forwarded += outcome.status == "forwarded"
            duplicates += outcome.status == "duplicate"
            dead_lettered += outcome.new_dead_letter
            pending_held += outcome.status == "submission-held"
        pending_remaining = self._state.pending_inbound_submission_count()
        if self._logger is not None and (pending_attempted or pending_remaining):
            try:
                self._logger.info("adapter.reconcile.pending", attempted=pending_attempted,
                                  remaining=pending_remaining, held=pending_held)
            except (NameError, ImportError):
                raise
            except Exception:
                pass
        chats = self._state.recent_chats()
        self._log_sweep_started(len(chats))
        for chat_id in chats:
            try:
                batch = self._lark.list_chat_messages(
                    chat_id,
                    start_time=start_time,
                    chat_type=self._state.chat_type(chat_id),
                )
            except (NameError, ImportError):
                raise
            except LarkApiError as error:
                # The platform already told us *why*, in fields: a permission
                # rejection arrives with the scopes it wants. "Never copy SDK
                # error text" (below) forbids the free-text message; it never
                # asked us to discard the structured fields, and discarding
                # them is what left the caller unable to tell a failure that
                # will clear from one that never can.
                if error.missing_scopes:
                    # A missing scope is the ONE permanent-looking failure a
                    # human can clear -- by granting it.  So it is named and
                    # reported, but the chat stays in the scan set: retiring it
                    # would make the eventual grant invisible forever.
                    blocked.append(
                        f"{chat_id}: missing-scope "
                        + "+".join(sorted(error.missing_scopes))
                    )
                    self._log_blocked_chat(chat_id, error)
                    continue
                if error.code in _UNREADABLE_CHAT_CODES:
                    # Same bucket as a missing scope, for the same reason: the
                    # sweep will fail identically every time until a human acts,
                    # so failing the probe on it is a restart loop, not a gate.
                    # "Until a human acts" is also why it stays IN the scan set:
                    # the code names the permission a human can grant.
                    blocked.append(f"{chat_id}: unreadable {error.code}")
                    self._log_blocked_chat(chat_id, error)
                    continue
                if error.code in LARK_HISTORY_CODES_PERMANENT_CHAT_GONE:
                    # The one permanent class: nothing a human does brings this
                    # chat back (the bot is no longer a member), so it leaves the
                    # scan set as well as the fail-closed column.
                    blocked.append(f"{chat_id}: unreadable {error.code}")
                    self._log_blocked_chat(chat_id, error)
                    self._retire_chat(chat_id, code=error.code)
                    retired.append(chat_id)
                    continue
                # Still fails closed: an unrecognised code may well be
                # transient, and a sweep that might have missed messages must
                # not report health.  But say which code it was, or the only
                # way to learn that this one is permanent is to watch an
                # adapter quarantine itself and have nothing name the cause.
                self._log_unreadable_chat(chat_id, error.code)
                errors.append(f"{chat_id}: {_reconcile_error_category(error)}")
                continue
            except Exception as error:  # noqa: BLE001 - per-chat isolation
                # A scope/permission/rate failure for one chat must not
                # starve the others. Never copy SDK error text into state or
                # telemetry because it can contain URLs or access tokens.
                #
                # No retirement here, deliberately: a permanent-chat code is
                # only ever carried by a ``LarkApiError``, and every one of
                # those is caught by the handler above -- so a retirement path
                # in this branch would be unreachable code.  This branch is for
                # transport and shape failures, which are never permanent.
                self._log_unreadable_chat(chat_id, None)
                errors.append(f"{chat_id}: {_reconcile_error_category(error)}")
                continue
            if not getattr(batch, "complete", True):
                errors.append(f"{chat_id}: history-incomplete")
            for message in batch:
                if (message.message_id in attempted_ids
                        or self._state.pending_inbound_submission(message.message_id) is not None):
                    continue  # Frozen requests belong to the bounded replay pass.
                if _create_time_ms(message.create_time) < window_start_ms:
                    continue
                scanned += 1
                outcome = self.handle_inbound(message, suppress_guidance=True)
                if outcome.status == "forwarded":
                    forwarded += 1
                elif outcome.status == "duplicate":
                    duplicates += 1
                elif outcome.new_dead_letter:
                    dead_lettered += 1
                elif outcome.status in {
                    "no-target",
                    "route-unavailable",
                    "error",
                    "unsupported-message-type",
                    "invalid-message-content",
                }:
                    # The audit record already existed before this sweep. It
                    # remains operator-visible, but is not a newly missed
                    # websocket message and must not churn the subscription.
                    pass
                elif outcome.status not in {"ignored"}:
                    errors.append(f"{message.message_id}: {outcome.status}")
        return ReconcileReport(
            chats_scanned=len(chats),
            messages_scanned=scanned,
            forwarded=forwarded,
            duplicates=duplicates,
            dead_lettered=dead_lettered,
            retryable_errors=tuple(errors),
            blocked_chats=tuple(blocked),
            retired_chats=tuple(retired),
            pending_attempted=pending_attempted,
            pending_remaining=self._state.pending_inbound_submission_count(),
            pending_held=pending_held,
        )

    def _log_sweep_started(self, chat_count: int) -> None:
        """Mark that the sweep reached the loop, and over how many chats.

        This is the phase half of the failure telemetry: the emit that names
        the exception cannot compute the phase where it fires (the exception
        has already left ``reconcile_recent``), so the phase is read off this
        marker's presence instead.  ``chatCount`` also answers the open
        question of how many chats this adapter's scan set actually holds.
        """

        if self._logger is None:
            return
        try:
            self._logger.info(
                "adapter.reconcile.sweep_started",
                chatCount=chat_count,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def _log_blocked_chat(self, chat_id: str, error: LarkApiError) -> None:
        """Say which chat is unreadable and what would make it readable.

        Skipping silently would leave a permanently unreconciled chat looking
        exactly like a working one: the adapter reports healthy and nothing
        ever names the gap.  Only structured fields go out -- the scope names
        and the platform code -- never the SDK message, which can carry URLs
        or tokens.
        """

        if self._logger is None:
            return
        try:
            self._logger.warn(
                "adapter.reconcile.chat_blocked",
                chatId=chat_id,
                missingScopes=list(error.missing_scopes),
                platformCode=error.code,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def _log_unreadable_chat(self, chat_id: str, code: int | None) -> None:
        """Name the code behind a failure this sweep could not classify.

        These still fail the probe, so a run of them ends in a supervised
        rebuild and then quarantine.  Without this line that outcome carries
        no cause at all: the adapter dies, and the one fact needed to decide
        whether the code belongs in :data:`_UNREADABLE_CHAT_CODES` is the one
        fact nothing recorded.  Only the platform code goes out, never the SDK
        message, which can carry URLs or tokens.
        """

        if self._logger is None:
            return
        try:
            self._logger.warn(
                "adapter.reconcile.chat_unreadable",
                chatId=chat_id,
                platformCode=code,
                node="adapter-reconcile",
            )
        except Exception:  # noqa: BLE001 - telemetry must not break a sweep
            pass

    def dead_letters(self) -> tuple[DeadLetter, ...]:
        """The preserved bodies of inbound messages that never delivered."""

        return self._state.dead_letters()

    def _with_known_chat_type(self, message: LarkInboundMessage) -> LarkInboundMessage:
        if message.chat_type != "unknown":
            return message
        known = self._state.chat_type(message.chat_id)
        return replace(message, chat_type=known) if known else message

    def _record_dead_letter(
        self,
        message: LarkInboundMessage,
        *,
        reason: str,
        detail: str | None = None,
    ) -> bool:
        try:
            now = self._utcnow().astimezone(UTC)
            return self._persist_dead_letter(
                DeadLetter(
                    message_id=message.message_id,
                    event_id=message.event_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    conversation_id=message.conversation_id,
                    sender_id=message.sender_id,
                    sender_type=message.sender_type,
                    text=message.text,
                    reason=reason,
                    detail=detail,
                    created_at=now.isoformat(timespec="seconds"),
                    create_time=message.create_time,
                    reply_to=message.reply_to,
                    message_type=message.message_type,
                ),
                now=now,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - the audit trail must never break inbound
            return False

    def _dead_letter_unparsed(self, event: Any, error: Exception) -> None:
        """Preserve what we can of an event that failed normalization."""

        try:
            raw_event = getattr(event, "event", None)
            raw_message = getattr(raw_event, "message", None)
            raw_sender = getattr(raw_event, "sender", None)
            message_id = getattr(raw_message, "message_id", None)
            if not message_id:
                return  # nothing stable to key the record on
            header = getattr(event, "header", None)
            chat_id = getattr(raw_message, "chat_id", None) or ""
            safe_message_type = canonical_lark_message_type(
                getattr(raw_message, "message_type", None)
            )
            now = self._utcnow().astimezone(UTC)
            self._persist_dead_letter(
                DeadLetter(
                    message_id=message_id,
                    event_id=getattr(header, "event_id", None) or "",
                    chat_id=chat_id,
                    chat_type=getattr(raw_message, "chat_type", None) or "unknown",
                    conversation_id=(
                        f"lark:{chat_id}:{chat_id}" if chat_id else ""
                    ),
                    sender_id=(
                        getattr(
                            getattr(raw_sender, "sender_id", None), "open_id", None
                        )
                        or "unknown"
                    ),
                    sender_type=getattr(raw_sender, "sender_type", None) or "unknown",
                    text=f"[Unreadable Lark {safe_message_type} message]",
                    reason="normalize-error",
                    detail=type(error).__name__,
                    created_at=now.isoformat(timespec="seconds"),
                    create_time=getattr(raw_message, "create_time", None),
                    reply_to=getattr(raw_message, "parent_id", None),
                    message_type=safe_message_type,
                ),
                now=now,
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - the audit trail must never break inbound
            pass

    def _persist_dead_letter(self, letter: DeadLetter, *, now: datetime) -> bool:
        """Persist once, then expose references without ever exposing the body."""

        created = self._state.record_dead_letter(letter)
        if not created:
            return False
        if self._logger is not None:
            try:
                self._logger.log(
                    "warn",
                    "dead-letter",
                    reason=letter.reason,
                    message_id=letter.message_id,
                    chat_id=letter.chat_id,
                    chat_type=letter.chat_type,
                    conversation_id=letter.conversation_id,
                )
            except (NameError, ImportError):
                raise
            except Exception:  # noqa: BLE001,S110 - visibility is best effort
                pass
        unconfirmed = letter.reason == "forward-outcome-unknown"
        # Audit/window expiry must not re-arm the pending request's notice.
        # Consume its claim before native I/O; never undo a failed attempt.
        if (self._alarm is not None and not self._closing.is_set()
                and (not unconfirmed or self._state.claim_pending_inbound_notice(letter.message_id))
                and not self._closing.is_set()):
            self._alarm.emit(
                Alarm(
                    correlation_id=letter.message_id,
                    message_id=letter.message_id,
                    conversation_id=letter.conversation_id,
                    sender=self._channel_actor_id,
                    recipient="lark-native-conversation",
                    reason=letter.reason,
                    audience="human",
                    text=(
                        "消息投递尚未确认，对方可能已经收到。原始请求已保留以便核验，请勿重复发送。"
                        if unconfirmed else None
                    ),
                ),
                terminal=not unconfirmed,
            )
        if not self._closing.is_set():
            self._maybe_notify_dead_letter_backlog(now=now)
        return True

    def _deliver_native_alarm(self, alarm: Alarm, text: str) -> bool:
        self._lark.reply(
            alarm.message_id,
            text,
            idempotency_key=f"alarm:{alarm.message_id}:{alarm.reason}",
        )
        return True

    def _maybe_notify_dead_letter_backlog(self, *, now: datetime) -> None:
        notifier = self._notify_operator
        if notifier is None:
            return
        window_epoch = (
            int(now.timestamp()) // DEAD_LETTER_ALERT_WINDOW_SECONDS
        ) * DEAD_LETTER_ALERT_WINDOW_SECONDS
        window_start = datetime.fromtimestamp(window_epoch, UTC).isoformat(
            timespec="seconds"
        )
        try:
            count, reasons = self._state.dead_letter_summary(since=window_start)
            if count < self._dead_letter_alert_threshold:
                return
            claimed = self._state.claim_dead_letter_alert(
                window_start=window_start,
                claimed_at=now.isoformat(timespec="seconds"),
            )
            if not claimed:
                return
            reason_summary = ",".join(
                f"{reason}:{reason_count}"
                for reason, reason_count in sorted(reasons.items())
            )
            text = f"Dead-letter backlog: count={count}; reasons={reason_summary}"
            correlation_id = (
                f"dead-letter-backlog:{self._adapter_name}:{window_epoch}"
            )
            if self._alarm is None:
                sent = notifier(text, correlation_id)
            else:
                outcome = self._alarm.emit(
                    Alarm(
                        correlation_id=correlation_id,
                        message_id=correlation_id,
                        conversation_id=f"operator:{self._adapter_name}",
                        sender=self._channel_actor_id,
                        recipient="operator-route",
                        reason="dead-letter-backlog",
                        audience="operator",
                    ),
                    delivery=lambda _alarm, _rendered: notifier(
                        text, correlation_id
                    ),
                    terminal=False,
                    throttle=False,
                )
                sent = outcome.status == "delivered"
            if not sent:
                self._state.release_dead_letter_alert(window_start=window_start)
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - operator notification is best effort
            try:
                self._state.release_dead_letter_alert(window_start=window_start)
            except (NameError, ImportError):
                raise
            except Exception:  # noqa: BLE001,S110 - retain original boundary
                pass
