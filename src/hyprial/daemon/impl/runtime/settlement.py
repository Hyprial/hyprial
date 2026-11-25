# Decision notes (2026-11-17, fail-loud review of conversation plan slice C1):
# - The in-channel notice once-key was (conversation, notice kind) for the
#   life of the process.  A send without a conversationId now joins the
#   default conversation of its pair, so that key would show a person one
#   no-progress notice per pair until the daemon restarts.  The key's first
#   part is failure_dedup_scope(conversation, the notice's deliveryId): the
#   request inside a derived default conversation, the conversation (an
#   adapter chat, an explicit id) as before everywhere else.  The notice's
#   own message id is per attempt and would defeat the retry collapse, so
#   the request is read from the payload's deliveryId.
"""Harness settlement records: inflight attempts, failure custody and notice recovery."""

from __future__ import annotations
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol
from hyprial.daemon.impl.inbox import InboxAuthorityUnavailable, InboxMessage
from hyprial.daemon.impl.inbox.tracking.progress import COALESCE_KEPT_PHASES, ProgressEvent
from hyprial.daemon import (
    AttemptIdentity,
)
from hyprial.daemon.impl.correlation.alarm import failure_dedup_scope
if TYPE_CHECKING:
    from hyprial.daemon.impl.cells.actor.contracts import CellDispatchStamp

from hyprial.kernel import DEFAULT_POLICIES, EXTERNAL_IO

_RETRY_PUMP_IDLE_SECONDS = DEFAULT_POLICIES[EXTERNAL_IO].max_backoff
_RETRY_PUMP_RETRY_FLOOR_SECONDS = DEFAULT_POLICIES[EXTERNAL_IO].base_backoff
_RETRY_PUMP_JOIN_SECONDS = 3.0
_RETRY_PUMP_SLOW_MS = 1000
HARNESS_FAILURE_MAX_ATTEMPTS = 3


HARNESS_FAILURE_BACKOFF_MS = (1_000, 5_000)


BLOCKING_FAILURE_CUSTODY_CAPACITY = 10_000



def _retry_pump_wait_seconds(inbox: object, *, now_ms: int) -> float:
    """Wait until the next durable due key, capped by the policy ceiling."""

    due_reader = getattr(inbox, "next_retry_due_ms", None)
    due_ms = due_reader() if callable(due_reader) else None
    if due_ms is None:
        return _RETRY_PUMP_IDLE_SECONDS
    return min(_RETRY_PUMP_IDLE_SECONDS, max(0.0, (due_ms - now_ms) / 1_000))


@dataclass(frozen=True, slots=True)
class _InflightAttempt:
    """One request currently handed to (or waiting for) a worker.

    This is the clock for the no-event path: the deadline runs from the
    moment the request entered the worker's queue, and the *only* thing that
    resets it is a progress observation correlated with this delivery.  A
    process being alive, online, or reconnecting is not progress and does not
    reset it (see ``_publish_harness_progress``).

    Decision record (cell model I5-2 C, second review round): ``stamp`` is
    the cell dispatch stamp of the envelope this attempt was offered under,
    bound when the attempt is created.  A steer reuses exactly this stamp,
    so the member a stalled delivery is steered to is the one that admitted
    the turn, never a fresh snapshot read; an attempt without a stamp was
    never offered through a cell decision and steers nothing.
    """

    identity: AttemptIdentity
    agent_entity_token: str | None
    budget_ms: int
    last_progress_ms: int
    reported: bool = False
    #: Set once this attempt's queued delivery was folded into the running
    #: turn by a native steer.  "Steer once" is a policy cap, not a retry
    #: loop: a second silence window after a successful steer reports instead.
    steered: bool = False
    #: The connector NAME (launch spec name) this attempt was handed to.  The
    #: identity keeps the canonical worker URI for evidence; the steer port is
    #: addressed by name, and a reverse URI parse would be a second lookup.
    worker_name: str = ""
    stamp: CellDispatchStamp | None = None


@dataclass(frozen=True, slots=True)
class _QueuedDeliveryHold:
    """Last hold refresh for one delivery accepted by a live worker queue."""

    worker: str
    refreshed_at_ms: int


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


def _notice_kind(message: InboxMessage) -> str | None:
    """The ``notification`` kind of a fail-loud notice, for its log line."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return None
    kind = body.get("notification") if isinstance(body, dict) else None
    return kind if isinstance(kind, str) else None


def _in_channel_notice_key(message: InboxMessage) -> tuple[str, str]:
    """The once-key of an in-channel fail-loud notice: (scope, notice kind)."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        body = None
    fields = body if isinstance(body, dict) else {}
    kind = fields.get("notification")
    delivery = fields.get("deliveryId")
    return (
        failure_dedup_scope(
            message.conversation_id, delivery if isinstance(delivery, str) else None
        ),
        kind if isinstance(kind, str) else "",
    )


def _notice_text(message: InboxMessage) -> str:
    """The human-readable ``message`` of a fail-loud notice (else the raw body)."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return str(message.payload)
    text = body.get("message") if isinstance(body, dict) else None
    return text if isinstance(text, str) else json.dumps(body, ensure_ascii=False)


_OWNER_HIDDEN_NOTICE_LABELS = ("- worker:", "- 静默预算:", "- attempt:")


def _owner_facing_notice_text(message: InboxMessage) -> str:
    """The notice text with its operator-only fields dropped."""

    return "\n".join(
        line
        for line in _notice_text(message).splitlines()
        if not line.startswith(_OWNER_HIDDEN_NOTICE_LABELS)
    )


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


def _answer_already_committed(error: BaseException) -> bool:
    """A mechanism answer submit refused because it is already durable.

    The mechanism answer functions derive the reply id from the delivery they
    answer, so the authority's ``SUBMISSION_RECEIPT_CONFLICT`` on it means the
    answer already committed (typically: the first submit timed out after the
    answer was sent).  Same raise shape as ``_stale_fence_rejection``.
    """

    return isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
        "SUBMISSION_RECEIPT_CONFLICT"
    )


def _coalesce_progress_events(
    events: tuple[ProgressEvent, ...],
) -> tuple[ProgressEvent, ...]:
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


class _NoticeRecoveryMixin:
    """Sender-notice durability across restart and shutdown.

    Split out of ``forwards.py`` (structural 1000-line gate); the methods run
    on ``DaemonEventBridge`` and rely on its ``inbox``, ``_logger``,
    ``_pending_notices`` and the notice helpers of ``_BridgeForwardsMixin``.
    """

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
