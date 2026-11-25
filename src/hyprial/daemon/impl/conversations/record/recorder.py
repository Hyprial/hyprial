# hyprial-unbounded-wait-allow: get - _LazyAuthority.get only resolves the recording authority; it is not a queue or future wait
"""``ConversationRecorder``: the write side of conversation records (plan §2.6).

Decision notes (phase A, S6; merged plan §2.0, §3 "Drop gating", Q5, Q9):

- The recorder is a background actor with its own thread.  Delivery never
  waits on it: it only reads committed rows through
  ``interaction_rows_after`` and pushes ``grant_body_release`` afterwards.
- Rows are read per source (``inbox`` then ``sender_copies``) from a cursor
  persisted in ``conversation-records.sqlite3``, up to 256 per page, and
  the cursor continues from ``page.cursor`` (which already steps over rows
  the facade does not hand out).
- The facade hands out every conversational row (``request`` and
  ``reply``) of both classes (plan slice S1, R6); the class is a field of
  the entry, not a reason to skip.  The row is filed under
  ``conversation_attribution``: the envelope thread stamp, else
  ``thread_id_for``, else the pair key of its two ends.  A row with none
  of them (a ``system:`` or bare-name end) is skipped as ``unthreaded``
  and keeps its body; a ``thread_id_for`` that raises (the thread
  authority is unavailable) delays the row instead, so an outage never
  files a threaded row under its pair key.
- Each row becomes a draft and passes the redaction hook.  A hook that
  raises delays the row (the hook may recover), so a broken hook never
  writes content and never makes a refusal permanent by accident.
- Only then is the recording authority resolved, once per pass and only
  for a row that needs one: the authority callable is never called by a
  pass that reads no threaded row, so an idle daemon composes nothing.
  A conversation is not a cell (owner ruling 2026-11-16): the recorder
  creates and ensures no cell document, never touches the cell runtime,
  and appends the entry straight to the segment under
  ``cells/conversation/<c.id>/rec-<writer>/`` in the owner-home space,
  where the ``c.`` id is ``conversation_cell_id`` of the local authority.
- Only a record that read back byte-identical with the original payload
  digest is granted; a grant carries the per-agent
  ``delivery.bodyRetentionMs`` from ``retention_for`` (subject: the
  recipient for an inbox row, the sender for a sender copy).  A SYSTEM row
  is granted the same way; delivery keeps it on the code default.
- An authority that cannot be resolved (OrgFS not composed, the placement
  closed) means nothing is recorded or granted, pending grant retries
  included.  Any OrgFS failure is ``RecordUnavailable``: the cursor stops
  before the first unrecorded row and the thread retries with exponential
  backoff (1 s doubling to 60 s).  Rows already recorded are known by
  ``(source, position, payload sha)``, so a retry never appends twice.
- A grant that raised stays ``recorded`` and is retried at the start of a
  later pass, at most ``GRANT_RETRY_BATCH`` per pass, oldest attempt first;
  every failed attempt moves the row behind all other pending grants, so a
  grant that keeps failing never starves newer ones.
- A pass checks ``close`` between sources and between cell groups and stops
  before the next append.
- The thread wakes on an inbox version change (polled every ``poll_s``, no
  I/O), on ``kick`` and every ``idle_s``; ``close`` joins it within its
  timeout and closes the local store.

Decision notes (plan slice S2, owner ruling 2026-11-17):

- A row the hook refuses (None: ``redacted``; a non-bytes body:
  ``redaction-invalid``) or whose entry would exceed ``ENTRY_MAX_BYTES``
  (``too-large``) is recorded as a placeholder (``RecordEntry.placeholder``):
  the row's identity and metadata, no body, the reason.  It is built from
  the row, never from what the hook returned.  A placeholder is never
  granted, so the queue keeps the row's body until the row itself is
  pruned; its status is ``redacted`` like any entry that is not the
  original body.  A refused row waits for the recording authority like
  any other row.  (The ``cell-<code>`` / ``cell-id-mismatch`` faults and
  the ``conversation.cell_fault`` log went away with the cell document:
  there is no cell to refuse or mismatch any more.)
- Rows skipped before placeholders existed stay skipped (the cursor never
  goes back); only a lost local index, which restarts the cursor, gives
  them placeholders.

Decision notes (UA2, 2026-11-18):

- An optional ``RecordObserver`` hears of every inbox row recorded intact
  (status ``recorded``, then granted or refused) and, since UA2b, of every
  inbox row recorded without its exact body (status ``redacted``: a
  placeholder or a redacted body; ``intact`` false), after the record is
  written and read back.  A skipped row is never observed.  Composition
  uses it to acknowledge rows addressed to ``user:<owner>``, whose durable
  copy is the record.
- The hook is edge-triggered, so a recorder also replays it once for the
  rows the observer still awaits (``awaiting``) that the index already
  shows recorded: a stop between the record and the hook is healed at the
  next start.  That scan reads the inbox source up to its cursor and runs
  only when something is awaited.  While ``awaiting`` answers None (the
  observer cannot know yet, UA2b item 6) or the scan fails, the replay is
  tried again on the next pass.
- UA2b item 5: a hook that raises never fails the record it heard of.  Its
  row is kept (bounded, ``OBSERVER_RETRY_MAX``) and handed to the hook again
  on later passes, at most ``OBSERVER_RETRY_PER_PASS`` per pass, dropping
  rows the observer no longer awaits; nothing rescans the feed.  A pass
  whose hook failed counts as a failed pass for the backoff
  (``retry_delay_s``), and the loop keeps wanting a pass while rows wait.
- Slice GC4 (group chat draft §4.6): a delivered group row (its
  conversation id a ``g.`` group id) is skipped as ``group-document``
  before any keying: the group document already holds the message.
- UA2c: a row whose hook failed ``OBSERVER_ATTEMPT_MAX`` times is dropped
  from the retry with an ``observer_gave_up`` log line, so one row the hook
  can never handle does not hold the recorder in backoff; the row itself
  stays queued for the hold TTL.  ``DaemonRestoringError`` is "retry later"
  and does not count, so a long restore cannot use up the attempts.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from hyprial.daemon.impl.conversations.record.contracts import (
    LOG_COMPONENT,
    RECORDER_THREAD,
    InteractionFeed,
    Logger,
    RecordDraft,
    RecordEntry,
    RecordRef,
    RecordUnavailable,
    Redaction,
    RetentionFor,
    ThreadIdFor,
    conversation_attribution,
    conversation_cell_id,
    entry_key,
)
from hyprial.daemon.impl.conversations.record.redaction import no_redaction
from hyprial.daemon.impl.conversations.record.segments import (
    ENTRY_MAX_BYTES,
    SegmentStore,
)
from hyprial.daemon.impl.conversations.record.store import (
    SKIPPED_PREFIX,
    STATUS_GRANTED,
    STATUS_RECORDED,
    STATUS_REDACTED,
    STATUS_REFUSED,
    RecordStore,
)
from hyprial.daemon.impl.delivery.ports import (
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
    InteractionRow,
)
from hyprial.kernel import ipc_errors, is_group_id_segment

FEED_SOURCES = (SOURCE_INBOX, SOURCE_SENDER_COPY)
PAGE_LIMIT = 256
GRANT_RETRY_BATCH = 32
RETRY_BASE_S = 1.0
RETRY_MAX_S = 60.0
POLL_S = 0.5
IDLE_S = 30.0
OMITTED_REDACTED = "redacted"
OMITTED_REDACTION_INVALID = "redaction-invalid"
OMITTED_TOO_LARGE = "too-large"
_INTACT = frozenset({STATUS_RECORDED, STATUS_GRANTED, STATUS_REFUSED})
OBSERVER_RETRY_MAX = 1024
OBSERVER_RETRY_PER_PASS = 32
OBSERVER_ATTEMPT_MAX = 8


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _no_retention(_row: InteractionRow) -> int | None:
    return None


def retry_delay_s(failures: int) -> float:
    """The wait before the next attempt after *failures* failed passes."""

    if failures <= 0:
        return 0.0
    return min(RETRY_MAX_S, RETRY_BASE_S * 2 ** min(failures - 1, 16))


class RecordObserver(Protocol):
    """Hears of inbox rows recorded intact (UA2)."""

    def awaiting(self) -> frozenset[str] | None:
        """Message ids of inbox rows the observer still waits to hear of.

        None while the observer cannot know yet; the replay then waits.
        """
        ...

    def recorded(self, row: InteractionRow, *, intact: bool) -> None:
        """One inbox row whose record was written and read back.

        *intact* is false when the record holds a placeholder or a redacted
        body instead of the row's exact payload.
        """
        ...


@dataclass(frozen=True, slots=True)
class RecordPass:
    """What one recorder pass did."""

    recorded: int = 0
    granted: int = 0
    skipped: int = 0
    failed: bool = False
    unavailable: bool = False
    more: bool = False
    omitted: int = 0


@dataclass(slots=True)
class _Tally:
    recorded: int = 0
    granted: int = 0
    skipped: int = 0
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class _Prepared:
    row: InteractionRow
    cell_id: str
    entry: RecordEntry


class _LazyAuthority:
    """The pass's recording authority, resolved on first need."""

    def __init__(self, authority: Callable[[], str]) -> None:
        self._authority = authority
        self._resolved = False
        self._value: str | None = None

    @property
    def unavailable(self) -> bool:
        return self._resolved and self._value is None

    def get(self) -> str:
        if not self._resolved:
            self._resolved = True
            try:
                value = self._authority()
            except RecordUnavailable:
                raise
            except Exception:  # noqa: BLE001 - an unreachable authority only delays records
                value = None
            if isinstance(value, str) and value.startswith("user:"):
                self._value = value
        if self._value is None:
            raise RecordUnavailable("authority")
        return self._value


class _Skip(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConversationRecorder:
    """Records delivered conversational bodies into conversation records."""

    def __init__(
        self,
        *,
        feed: InteractionFeed,
        authority: Callable[[], str],
        segments: SegmentStore,
        store: RecordStore,
        thread_id_for: ThreadIdFor,
        retention_for: RetentionFor = _no_retention,
        redact: Redaction = no_redaction,
        clock_ms: Callable[[], int] = _wall_ms,
        logger: Logger | None = None,
        version: Callable[[], int] | None = None,
        page_limit: int = PAGE_LIMIT,
        poll_s: float = POLL_S,
        idle_s: float = IDLE_S,
        observer: RecordObserver | None = None,
    ) -> None:
        if not 1 <= page_limit <= PAGE_LIMIT:
            raise ValueError(f"page_limit must be in 1..{PAGE_LIMIT}")
        self._feed = feed
        self._authority = authority
        self._segments = segments
        self._store = store
        self._thread_id_for = thread_id_for
        self._retention_for = retention_for
        self._redact = redact
        self._clock = clock_ms
        self._logger = logger
        self._version = version or (lambda: 0)
        self._page_limit = page_limit
        self._poll_s = poll_s
        self._idle_s = idle_s
        self._observer = observer
        self._replayed = observer is None
        self._observe_retry: OrderedDict[
            tuple[str, int, str], tuple[InteractionRow, bool, int]
        ] = OrderedDict()
        self._observer_failed = False
        self._pass_lock = threading.Lock()
        self._failures = 0
        self._failing = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def failures(self) -> int:
        return self._failures

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=RECORDER_THREAD, daemon=True)
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def close(self, timeout: float = 5.0) -> bool:
        """Stop the thread and close the store; True when it stopped in time."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        with self._pass_lock:
            self._store.close()
        return True

    def _loop(self) -> None:
        seen: int | None = None
        more = False
        not_before = 0.0
        next_idle = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            version = self._safe_version()
            wanted = (
                more or version != seen or now >= next_idle or self._wake.is_set()
                or bool(self._observe_retry)
            )
            self._wake.clear()
            if wanted and now >= not_before:
                seen = version
                result = self.run_once()
                more = result.more and not result.failed
                not_before = now + retry_delay_s(self._failures)
                next_idle = now + self._idle_s
                if more:
                    continue
            self._wake.wait(self._poll_s)

    def _safe_version(self) -> int | None:
        try:
            return int(self._version())
        except Exception:  # noqa: BLE001 - a version probe never stops the actor
            return None

    def run_once(self) -> RecordPass:
        """One bounded pass: retry grants, then record one page per source."""

        with self._pass_lock:
            if self._stop.is_set():
                return RecordPass()
            try:
                return self._pass()
            except Exception as error:  # noqa: BLE001 - a pass never kills the actor
                self._log("warn", "conversation.pass_failed", errorType=type(error).__name__,
                          detail=str(error)[:300])
                self._failed("pass-failed")
                return RecordPass(failed=True)

    def _pass(self) -> RecordPass:
        authority = _LazyAuthority(self._authority)
        tally = _Tally()
        self._observer_failed = False
        if not self._replayed:
            self._replay_observer()
        self._retry_observer()
        failure = self._retry_grants(authority, tally)
        more = False
        for source in FEED_SOURCES:
            if self._stop.is_set():
                break
            found, scanned = self._record_source(authority, source, tally)
            more = more or scanned
            if found is not None:
                failure = found
                break
        if failure is None and self._observer_failed:
            failure = "observer-failed"
        if failure is not None:
            self._failed(failure)
        elif self._failing:
            self._failing = False
            self._failures = 0
            self._log("info", "conversation.record_resumed")
        else:
            self._failures = 0
        return RecordPass(
            recorded=tally.recorded,
            granted=tally.granted,
            skipped=tally.skipped,
            omitted=tally.omitted,
            failed=failure is not None,
            unavailable=authority.unavailable,
            more=more and failure is None,
        )

    def _failed(self, reason: str) -> None:
        self._failures += 1
        if not self._failing:
            self._failing = True
            self._log("warn", "conversation.record_unavailable", reason=reason[:200])

    def _record_source(
        self, authority: _LazyAuthority, source: str, tally: _Tally
    ) -> tuple[str | None, bool]:
        after = self._store.cursor(source)
        page = self._feed.interaction_rows_after(source, after, limit=self._page_limit)
        groups: dict[str, list[_Prepared]] = {}
        failed_at: int | None = None
        failure: str | None = None
        for row in page.rows:
            if self._store.status(row.source, row.position, row.payload_sha256) is not None:
                continue
            try:
                prepared = self._prepare(authority, row)
            except _Skip as skip:
                self._skip(row, skip, tally)
                continue
            except RecordUnavailable as error:
                failed_at, failure = row.position, str(error) or "authority"
                break
            groups.setdefault(prepared.cell_id, []).append(prepared)
        for cell_id, items in groups.items():
            if failure is not None or self._stop.is_set():
                first = items[0].row.position
                failed_at = first if failed_at is None else min(failed_at, first)
                continue
            try:
                recorded, seq = self._segments.append(
                    authority.get(), cell_id, [item.entry for item in items],
                    self._store.segment(cell_id),
                )
            except RecordUnavailable as error:
                failed_at, failure = items[0].row.position, str(error) or "orgfs"
                continue
            self._store.set_segment(cell_id, seq)
            for item in items:
                ref, stored = recorded[item.entry.key]
                self._settle(item, ref, stored, tally)
        if failed_at is not None:
            self._store.set_cursor(source, max(after, failed_at - 1))
        else:
            self._store.set_cursor(source, page.cursor)
        return failure, page.cursor > after

    def _prepare(self, authority: _LazyAuthority, row: InteractionRow) -> _Prepared:
        message = row.message
        if is_group_id_segment(message.conversation_id or ""):
            raise _Skip("group-document")
        thread_id = conversation_attribution(
            message.thread_id or self._thread_of(message.sender, message.recipient),
            message.sender,
            message.recipient,
        )
        if not thread_id:
            raise _Skip("unthreaded")
        conversation_id = message.conversation_id or ""
        draft = RecordDraft(
            source=row.source,
            position=row.position,
            payload_sha256=row.payload_sha256,
            message=message,
            thread_id=thread_id,
            conversation_id=conversation_id,
            body=bytes(message.payload),
        )
        try:
            kept = self._redact(draft)
        except Exception as error:  # noqa: BLE001 - a broken hook never writes content
            self._log("warn", "conversation.redaction_failed", errorType=type(error).__name__)
            raise RecordUnavailable("redaction-failed") from error
        if kept is None:
            omitted, body = OMITTED_REDACTED, b""
        elif not isinstance(kept.body, bytes):
            omitted, body = OMITTED_REDACTION_INVALID, b""
        else:
            omitted, body = None, kept.body
        cell_id = conversation_cell_id(authority.get(), thread_id, conversation_id)
        entry = RecordEntry(
            key=entry_key(row.source, row.position, row.payload_sha256),
            source=row.source,
            position=row.position,
            message_id=message.message_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            cell_id=cell_id,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            created_at_ms=int(message.created_at_ms),
            recorded_at_ms=int(self._clock()),
            payload_sha256=row.payload_sha256,
            body=body,
            redacted=body != draft.body,
            message_class=row.message_class.value,
        )
        if omitted is None and len(entry.to_line().encode("utf-8")) > ENTRY_MAX_BYTES:
            omitted = OMITTED_TOO_LARGE
        if omitted is not None:
            entry = entry.placeholder(omitted)
        return _Prepared(row, cell_id, entry)

    def _thread_of(self, sender: str, recipient: str) -> str | None:
        try:
            thread_id = self._thread_id_for(sender, recipient)
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - an unavailable authority only delays
            raise RecordUnavailable("thread-authority") from error
        return thread_id if isinstance(thread_id, str) and thread_id else None

    def _skip(self, row: InteractionRow, skip: _Skip, tally: _Tally) -> None:
        self._store.mark(row.source, row.position, row.payload_sha256,
                         f"{SKIPPED_PREFIX}{skip.reason}", now_ms=self._clock())
        tally.skipped += 1
        self._log("info", "conversation.row_skipped", source=row.source,
                  position=row.position, reason=skip.reason)

    def _settle(
        self, item: _Prepared, ref: RecordRef, stored: RecordEntry, tally: _Tally
    ) -> None:
        row = item.row
        text = ref.encode()
        tally.recorded += 1
        if item.entry.body_omitted is not None:
            tally.omitted += 1
            self._log("info", "conversation.body_omitted", source=row.source,
                      position=row.position, reason=item.entry.body_omitted)
        intact = (
            not stored.redacted
            and stored.body_omitted is None
            and stored.payload_sha256 == row.payload_sha256
            and hashlib.sha256(stored.body).hexdigest() == row.payload_sha256
        )
        status = STATUS_RECORDED if intact else STATUS_REDACTED
        self._store.mark(row.source, row.position, row.payload_sha256, status,
                         now_ms=self._clock(), cell_id=item.cell_id, ref=text)
        if intact:
            self._grant(row.source, row.position, row.payload_sha256, text, row, tally)
        self._observe(row, intact)

    def _observe(self, row: InteractionRow, intact: bool) -> None:
        if self._observer is None or row.source != SOURCE_INBOX:
            return
        key = (row.source, row.position, row.payload_sha256)
        _, _, attempts = self._observe_retry.pop(key, (row, intact, 0))
        try:
            self._observer.recorded(row, intact=intact)
        except Exception as error:  # noqa: BLE001 - an observer never fails a record
            self._observer_failed = True
            if not isinstance(error, ipc_errors.DaemonRestoringError):
                attempts += 1
            if attempts >= OBSERVER_ATTEMPT_MAX:
                self._log("warn", "conversation.observer_gave_up", position=row.position,
                          messageId=row.message.message_id, attempts=attempts,
                          errorType=type(error).__name__)
                return
            self._observe_retry[key] = (row, intact, attempts)
            if len(self._observe_retry) > OBSERVER_RETRY_MAX:
                self._observe_retry.popitem(last=False)
                self._log("warn", "conversation.observer_retry_dropped")
            self._log("warn", "conversation.observer_failed", position=row.position,
                      errorType=type(error).__name__)

    def _retry_observer(self) -> None:
        """Hand the observer a bounded batch of the rows its hook failed on."""

        if self._observer is None or not self._observe_retry:
            return
        try:
            awaited = self._observer.awaiting()
        except Exception as error:  # noqa: BLE001 - tried again on the next pass
            self._observer_failed = True
            self._log("warn", "conversation.observer_retry_failed",
                      errorType=type(error).__name__)
            return
        if awaited is None:
            return
        for key, (row, _, _) in list(self._observe_retry.items()):
            if row.message.message_id not in awaited:
                del self._observe_retry[key]
        for row, intact, _ in list(self._observe_retry.values())[:OBSERVER_RETRY_PER_PASS]:
            if self._stop.is_set():
                return
            self._observe(row, intact)

    def _replay_observer(self) -> None:
        """Hand the observer the awaited rows already recorded, once.

        Done once ``awaiting`` answered and the scan finished; a hook that
        fails during it leaves its row to ``_retry_observer``.
        """

        if self._observer is None:
            self._replayed = True
            return
        try:
            awaited = self._observer.awaiting()
            if awaited is None:
                return
            if not awaited:
                self._replayed = True
                return
            end, after = self._store.cursor(SOURCE_INBOX), 0
            while after < end and not self._stop.is_set():
                page = self._feed.interaction_rows_after(
                    SOURCE_INBOX, after, limit=self._page_limit
                )
                for row in page.rows:
                    if row.position > end or row.message.message_id not in awaited:
                        continue
                    status = self._store.status(row.source, row.position, row.payload_sha256)
                    if status in _INTACT or status == STATUS_REDACTED:
                        self._observe(row, status in _INTACT)
                if page.cursor <= after:
                    break
                after = page.cursor
            self._replayed = not self._stop.is_set()
        except Exception as error:  # noqa: BLE001 - retried on the next pass
            self._log("warn", "conversation.observer_replay_failed",
                      errorType=type(error).__name__)

    def _retention(self, row: InteractionRow | None) -> int | None:
        if row is None:
            return None
        try:
            value = self._retention_for(row)
        except Exception:  # noqa: BLE001 - a broken source falls back to the bound one
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _grant(
        self,
        source: str,
        position: int,
        payload_sha256: str,
        ref: str,
        row: InteractionRow | None,
        tally: _Tally,
    ) -> None:
        grant = BodyGrant(source, position, payload_sha256, ref,
                          retention_ms=self._retention(row))
        try:
            accepted = bool(self._feed.grant_body_release(grant))
        except Exception as error:  # noqa: BLE001 - the grant is retried on a later pass
            self._log("warn", "conversation.grant_failed", source=source,
                      position=position, errorType=type(error).__name__)
            self._store.defer(source, position, payload_sha256, now_ms=self._clock())
            return
        self._store.mark(source, position, payload_sha256,
                         STATUS_GRANTED if accepted else STATUS_REFUSED, now_ms=self._clock())
        if accepted:
            tally.granted += 1

    def _retry_grants(self, authority: _LazyAuthority, tally: _Tally) -> str | None:
        pending_grants = self._store.pending_grants(GRANT_RETRY_BATCH)
        if not pending_grants:
            return None
        try:
            authority.get()
        except RecordUnavailable as error:
            return str(error) or "authority"
        for pending in pending_grants:
            if self._stop.is_set():
                break
            try:
                row = self._feed.interaction_row_at(pending.source, pending.position)
            except Exception:  # noqa: BLE001 - the grant stays pending
                self._store.defer(pending.source, pending.position, pending.payload_sha256,
                                  now_ms=self._clock())
                continue
            if row is not None and row.payload_sha256 != pending.payload_sha256:
                row = None
            self._grant(pending.source, pending.position, pending.payload_sha256,
                        pending.ref, row, tally)
        return None

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, LOG_COMPONENT, event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks a pass
            pass


__all__ = [
    "FEED_SOURCES",
    "GRANT_RETRY_BATCH",
    "IDLE_S",
    "OMITTED_REDACTED",
    "OMITTED_REDACTION_INVALID",
    "OMITTED_TOO_LARGE",
    "PAGE_LIMIT",
    "POLL_S",
    "RETRY_BASE_S",
    "RETRY_MAX_S",
    "ConversationRecorder",
    "RecordObserver",
    "RecordPass",
    "retry_delay_s",
]
