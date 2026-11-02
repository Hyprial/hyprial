"""Lark event conversion, routing, acknowledgement, and receipt correlation.

Decision block — 0.5.5.3 bounded inline backfill + sweep anchor (#2213
review 1344 Q1/Q2, Q1 rule revised per R&D lead addendum):
- The inline (live-message-triggered) backfill pulls history off the serial
  inbound effect lane and waits on it at most ``DEFAULT_INLINE_BACKFILL_SECONDS``
  (adapter.py); on expiry the chat pauses -- the live message is held as a
  ``backfill-unavailable`` dead letter with the ack refused (checkpoint not
  advanced) and never injected ahead of the unsettled history. The detached
  finisher replays the fetch's batch on the lane via ``ordered`` and then
  injects the held messages in arrival order, so the chat resumes in order
  while every other chat keeps flowing. ``fetch`` lets the sweep replay an
  already-pulled batch (or its error) instead of calling the SDK again; the
  scheduled path still fetches on the lane via ``reconcile_recent``.
- The sweep window anchors to ``chat_reconciliations`` (last cleanly swept
  point) in addition to ``chat_observations``, so a same-epoch gap older than
  the lookback survives later live messages advancing the observation mark.
"""

from __future__ import annotations

import heapq
import json
import time
from collections.abc import Callable
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
from hyprial.daemon.impl.adapters.lark.state.recovery import PENDING_RECOVERY_MAX_FAILURES
# Feishu returns either code when an app without broad group-history access
# attempts to list a chat's messages. Keep this allowlist specific to that one
# endpoint: any other Lark API failure must remain reconciliation-fatal.
_HISTORY_SCOPE_FORBIDDEN_CODES = frozenset({230002, 230027})
DEFAULT_DEAD_LETTER_ALERT_THRESHOLD = 10
DEAD_LETTER_ALERT_WINDOW_SECONDS = 60 * 60
MAX_BACKFILL_LOOKBACK_SECONDS = 24 * 60 * 60
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
    except (TypeError, ValueError):
        return 0


_HistoryCandidate = tuple[int, str, LarkInboundMessage | None]


def _ordered_candidates(
    candidates: dict[str, _HistoryCandidate],
    parents: dict[str, str | None],
) -> list[_HistoryCandidate]:
    """Use platform time, with parent-before-reply ordering for equal times."""
    children: dict[str, list[str]] = {}
    waiting: set[str] = set()
    for message_id, (created, _, message) in candidates.items():
        parent = (
            message.reply_to if message is not None else
            parents.get(message_id)
        )
        if parent in candidates:
            if candidates[parent][0] > created:
                raise ValueError("inconsistent history timestamps")
            waiting.add(message_id)
            children.setdefault(parent, []).append(message_id)
    ready = [(row[0], key) for key, row in candidates.items() if key not in waiting]
    heapq.heapify(ready)
    ordered: list[_HistoryCandidate] = []
    while ready:
        _, message_id = heapq.heappop(ready)
        ordered.append(candidates[message_id])
        for child in children.get(message_id, ()):
            heapq.heappush(ready, (candidates[child][0], child))
    if len(ordered) != len(candidates):
        raise ValueError("cyclic history replies")
    return ordered


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
            self._state.resume_pending_inbound(message_id)
            return self._recover_pending_submission(pending)
        message = self._lark.get_inbound_message(message_id)
        if message is None:
            return InboundOutcome(status="not-found")
        return self.handle_inbound(
            self._with_known_chat_type(message), suppress_guidance=True
        )

    def _recover_pending_submission(
        self, pending: tuple[str, str, str], *, deadline: float | None = None,
        ordered: bool = False,
    ) -> InboundOutcome:
        outcome = self._try_recover_pending_submission(pending, deadline=deadline, ordered=ordered)
        if (outcome.status not in {"error", "submission-held"}
                or outcome.ack_error in {"inbound-closing", "recovery-deadline"}):
            return outcome
        attempts = self._state.fail_pending_inbound(pending, now=self._utcnow())
        if attempts >= PENDING_RECOVERY_MAX_FAILURES:
            self._log_backfill("lark.backfill.dead-lettered", reason="submission-retry-exhausted",
                               attempts=attempts, acceptance="unconfirmed")
            return InboundOutcome(status="submission-dead-lettered", new_dead_letter=True)
        self._log_backfill("lark.backfill.deferred", reason="submission-recovery-failed", attempts=attempts)
        return outcome

    def _try_recover_pending_submission(
        self, pending: tuple[str, str, str], *, deadline: float | None = None,
        ordered: bool = False,
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
        if ordered:
            return self._handle_inbound(
                message, suppress_guidance=True, frozen_recovery=True,
                recovery_deadline=deadline,
            )
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
        if self._closing.is_set():
            return ReconcileReport(0, 0, 0, 0, 0, retryable_errors=("adapter: closing",))
        timeout = (
            PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS
            if self._state.pending_inbound_submission_count() else 30.0
        )
        try:
            return self._inbound.ordered(
                lambda: self._reconcile_recent_ordered(
                    lookback_seconds=lookback_seconds, now_ms=now_ms,
                ), timeout=timeout,
            )
        except TimeoutError:
            return ReconcileReport(
                0, 0, 0, 0, 0, retryable_errors=("adapter: recovery-deadline",),
                pending_remaining=self._state.pending_inbound_submission_count(),
            )

    def _history_window(
        self, chat_id: str, *, now_ms: int, lookback_seconds: int,
    ) -> tuple[int, int]:
        """Request and clamped window start for one chat's history pull.

        The window reaches back to the earlier of the configured lookback and
        the chat's durable progress points (last observation, last fully
        reconciled point), clamped to the 24-hour maximum lookback.
        """

        floor_ms = max(0, now_ms - MAX_BACKFILL_LOOKBACK_SECONDS * 1000)
        bounds = [now_ms - max(0, lookback_seconds) * 1000]
        observed = self._state.chat_observed_ms(chat_id)
        reconciled = self._state.chat_reconciled_ms(chat_id)
        if observed is not None:
            bounds.append(observed)
        if reconciled is not None:
            bounds.append(reconciled)
        requested_start = min(bounds)
        return requested_start, max(floor_ms, requested_start)

    def _reconcile_recent_ordered(
        self, *, lookback_seconds: int = DEFAULT_RECONCILE_LOOKBACK_SECONDS,
        now_ms: int | None = None, chat_ids: tuple[str, ...] | None = None,
        fetch: Callable[[str, str], Any] | None = None,
    ) -> ReconcileReport:
        """Replay messages a websocket outage dropped, per chat, idempotently.

        Scans durable chat observations, correlations, dead letters and pending
        submissions. The checkpoint expands the overlap window up to 24 hours.
        History replay runs on the same effect lane as live inbound work; when
        ``fetch`` is supplied it produces the batch for one chat from off-lane
        (the bounded inline leg replays an already-pulled batch this way),
        otherwise the SDK is called on the lane as before.
        Native message ID dedup makes repeat sweeps safe; guidance replies
        are suppressed so a still-
        unaddressed message is not re-answered on every reconnect.
        """

        if self._closing.is_set():
            return ReconcileReport(0, 0, 0, 0, 0, retryable_errors=("adapter: closing",))
        now_ms = int(self._utcnow().timestamp() * 1000) if now_ms is None else now_ms
        epoch = self._backfill_epoch
        floor_ms = max(0, now_ms - MAX_BACKFILL_LOOKBACK_SECONDS * 1000)
        window_starts: list[int] = []
        truncated = 0
        errors: list[str] = []
        blocked: list[str] = []
        retired: list[str] = []
        scanned = forwarded = duplicates = dead_lettered = 0
        pending_attempted = pending_held = 0
        attempted_ids: set[str] = set()
        headers = self._state.pending_inbound_headers()
        deferred_chats = {chat for _, chat, _, _ in headers if isinstance(chat, str) and chat
                          and self._state.retired_chat_code(chat) is None}
        deadline = time.monotonic() + PENDING_SUBMISSION_REPLAY_BUDGET_SECONDS
        for pending in self._state.pending_inbound_submission_page(limit=PENDING_SUBMISSION_REPLAY_BATCH):
            if self._closing.is_set() or time.monotonic() >= deadline:
                break
            try:
                pending_chat = json.loads(pending[2])["request"]["provider_metadata"]["chatId"]
            except (KeyError, TypeError, ValueError):
                pending_chat = None
            if chat_ids is not None and pending_chat not in chat_ids:
                continue
            if (isinstance(pending_chat, str) and pending_chat
                    and self._state.retired_chat_code(pending_chat) is None):
                continue
            self._state.advance_pending_inbound_cursor(pending[0])
            attempted_ids.add(pending[0])
            pending_attempted += 1
            scanned += 1
            outcome = self._recover_pending_submission(pending, deadline=deadline, ordered=True)
            forwarded += outcome.status == "forwarded"
            duplicates += outcome.status == "duplicate"
            dead_lettered += outcome.new_dead_letter
            pending_held += outcome.status == "submission-held"
            if outcome.status in {"error", "submission-held"}:
                errors.append("adapter: submission-recovery-failed")
        chats = (
            tuple(sorted(set(self._state.recent_chats()) | deferred_chats))
            if chat_ids is None else chat_ids
        )
        self._log_sweep_started(len(chats))
        for chat_id in chats:
            requested_start, window_start_ms = self._history_window(
                chat_id, now_ms=now_ms, lookback_seconds=lookback_seconds,
            )
            window_starts.append(window_start_ms)
            if requested_start < floor_ms:
                truncated += 1
                self._log_backfill("lark.backfill.truncated", windowStartMs=window_start_ms,
                                   observedMs=self._state.chat_observed_ms(chat_id),
                                   maxLookbackSeconds=MAX_BACKFILL_LOOKBACK_SECONDS)
            start_time = str(window_start_ms // 1000)
            errors_before = len(errors)
            try:
                batch = (
                    fetch(chat_id, start_time)
                    if fetch is not None
                    else self._lark.list_chat_messages(
                        chat_id,
                        start_time=start_time,
                        chat_type=self._state.chat_type(chat_id),
                    )
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
                continue
            candidates = {
                item.message_id: (_create_time_ms(item.create_time), item.message_id, item)
                for item in batch if window_start_ms <= _create_time_ms(item.create_time) <= now_ms
            }
            parents = {}
            for message_id, pending_chat, created, parent in headers:
                if pending_chat == chat_id:
                    candidates[message_id] = (_create_time_ms(created), message_id, None)
                    parents[message_id] = parent
            try:
                ordered = _ordered_candidates(candidates, parents)
            except ValueError:
                errors.append(f"{chat_id}: history-order-invalid")
                continue
            for _, message_id, message in ordered:
                if message_id in attempted_ids:
                    continue
                if self._state.pending_inbound_dead_lettered(message_id):
                    continue
                pending = self._state.pending_inbound_submission_record(message_id)
                if pending is not None:
                    if pending_attempted >= PENDING_SUBMISSION_REPLAY_BATCH or time.monotonic() >= deadline:
                        errors.append(f"{chat_id}: pending-recovery-budget")
                        self._log_backfill("lark.backfill.deferred", reason="pending-recovery-budget", attempts=pending_attempted)
                        break
                    self._state.advance_pending_inbound_cursor(message_id)
                    attempted_ids.add(message_id)
                    pending_attempted += 1
                    outcome = self._recover_pending_submission(pending, deadline=deadline, ordered=True)
                    pending_held += outcome.status == "submission-held"
                else:
                    outcome = self._handle_inbound(message, suppress_guidance=True)
                scanned += 1
                if outcome.status in {"error", "submission-held"}:
                    errors.append(f"{chat_id}: inbound-recovery-incomplete")
                    dead_lettered += outcome.new_dead_letter
                    break
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
                elif outcome.status not in {"ignored", "submission-dead-lettered"}:
                    errors.append(f"{message_id}: {outcome.status}")
                    break
            if len(errors) == errors_before and self._state.has_pending_inbound_chat(chat_id):
                errors.append(f"{chat_id}: pending-recovery-incomplete")
            if len(errors) == errors_before:
                self._state.observe_chat(chat_id, now_ms)
                self._state.mark_chat_reconciled(chat_id, now_ms)
                self._backfilled_chats[chat_id] = epoch
        pending_remaining = self._state.pending_inbound_submission_count()
        if (chat_ids is None and pending_remaining and not pending_attempted
                and not errors and not blocked):
            errors.append("adapter: pending-recovery-budget")
            self._log_backfill("lark.backfill.deferred", reason="pending-recovery-budget", attempts=0)
        if pending_attempted or pending_remaining:
            self._log_backfill("adapter.reconcile.pending", attempted=pending_attempted,
                               remaining=pending_remaining, held=pending_held)
        self._log_backfill(
            "lark.backfill.sweep", chatsScanned=len(chats),
            windowStartMs=min(window_starts, default=now_ms), windowEndMs=now_ms,
            forwarded=forwarded, duplicates=duplicates, truncated=truncated,
            retryableErrors=len(errors), blockedChats=len(blocked),
        )
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

    def _log_backfill(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            try:
                self._logger.info(event, **fields)
            except Exception:
                pass

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
