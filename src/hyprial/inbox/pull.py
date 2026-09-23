"""Durable, pullable terminal-state records for delivered messages.

Step 1 of the delivery-semantics rework (``notes/design-delivery-pull-model.md``
sections 5 and 6).  The X problem it closes: ``message.send`` returns
``accepted/queued/target-not-visible``, and that value carries **zero
information** about what happened to the message -- the same value has been
observed on a delivery that provably landed and on one that provably vanished.

The fix is not a better return value; a send-time answer cannot know a fact
that has not happened yet.  Instead every message ends in exactly one of two
**persisted** terminal states, written by whoever observed the outcome:

``fetched``
    The recipient daemon durably took the message.  Written at the recipient
    inside the same SQLite transaction that commits the message to its inbox,
    so it is a purely local fact -- no receipt leg, no network step can lose
    it.  Pull/fetch evidence is tracked separately to retire a sender outbox
    row that missed this commit receipt.

``expired``
    The holder evicted the message without it ever being fetched -- TTL,
    capacity, retry exhaustion or an undeliverable address.  Written by the
    holder inside the same transaction that removes the held row, so an
    eviction can never be silent.

Both are queryable **after the fact** over ``msg/status/<sender>``: a sender is
routinely offline at the moment its message reaches a terminal state, so a
one-shot notification would be exactly the unreliable leg this design removes.

Custody is ownership transfer, not replication (decision G1): a message moves
sender-outbox -> mailbox-custody, and only one holder exists at any instant, so
there is never a pair of records to reconcile.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import Any, TypeVar

from hyprial.transport import KeySpace, Registration, TransportSession

STATUS_FRAME_SCHEMA = "hyprial-delivery-status/v1"

# Section 6 defaults, decided by Allen 2026-08-14.  Time is the primary
# criterion because "stale" is a time concept; the item cap is a storage
# ceiling.  Both are per-daemon configurable through the environment.
DEFAULT_HOLD_TTL_MS = 30 * 60 * 1000
DEFAULT_HOLD_MAX_ITEMS = 100
# How long a terminal record stays pullable.  Not specified by the design; it
# only has to outlive the message TTL by a wide margin so a sender that was
# offline when its message terminated can still learn the outcome.
DEFAULT_STATUS_RETENTION_MS = 7 * 86_400_000


class TerminalState(StrEnum):
    """The only two ways a message may end.  Both are persisted."""

    FETCHED = "fetched"
    EXPIRED = "expired"


class HoldReason(StrEnum):
    """Why a terminal record was written.  Narrows the two states."""

    RECIPIENT_COMMIT = "RECIPIENT_COMMIT"
    RECIPIENT_DUPLICATE = "RECIPIENT_DUPLICATE"
    RECEIPT_CONFIRMED = "RECEIPT_CONFIRMED"
    HOLD_TTL_EXPIRED = "HOLD_TTL_EXPIRED"
    HOLD_CAPACITY_EXCEEDED = "HOLD_CAPACITY_EXCEEDED"


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    """One message's terminal outcome, as observed by ``holder``."""

    message_id: str
    sender: str
    recipient: str
    state: TerminalState
    holder: str
    reason: str
    recorded_at_ms: int
    conversation_id: str = ""
    idempotency_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "state": self.state.value,
            "holder": self.holder,
            "reason": self.reason,
            "recordedAtMs": self.recorded_at_ms,
            "conversationId": self.conversation_id,
            "idempotencyKey": self.idempotency_key,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> DeliveryStatus:
        return cls(
            message_id=str(value["messageId"]),
            sender=str(value["sender"]),
            recipient=str(value["recipient"]),
            state=TerminalState(str(value["state"])),
            holder=str(value["holder"]),
            reason=str(value["reason"]),
            recorded_at_ms=int(value["recordedAtMs"]),
            conversation_id=str(value.get("conversationId") or ""),
            idempotency_key=value.get("idempotencyKey"),
        )


def _positive_int(raw: str | None, fallback: int) -> int:
    if raw is None or not raw.strip():
        return fallback
    try:
        parsed = int(raw)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


@dataclass(frozen=True, slots=True)
class HoldPolicy:
    """How long, and how many, undelivered messages a holder keeps.

    Applied by the holder against **its own clock**, counted from the moment it
    took the message (section 6).  A sender's ``created_at_ms`` is never used
    for elapsed time: clocks across machines disagree, and computing your own
    timeout from someone else's clock yields negative ages or premature
    expiry -- the same class of error as ordering by send time (section 8.3).
    """

    ttl_ms: int = DEFAULT_HOLD_TTL_MS
    max_items: int = DEFAULT_HOLD_MAX_ITEMS
    status_retention_ms: int = DEFAULT_STATUS_RETENTION_MS

    def __post_init__(self) -> None:
        # A zero or negative bound would evict messages the instant they are
        # taken -- observably, but still a configuration that quietly destroys
        # traffic.  Refuse it at construction rather than at 3am.
        for label, value in (
            ("ttl_ms", self.ttl_ms),
            ("max_items", self.max_items),
            ("status_retention_ms", self.status_retention_ms),
        ):
            if value <= 0:
                raise ValueError(f"HoldPolicy.{label} must be positive, got {value}")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> HoldPolicy:
        env = os.environ if environ is None else environ
        return cls(
            ttl_ms=_positive_int(env.get("HYPRIAL_HOLD_TTL_MS"), DEFAULT_HOLD_TTL_MS),
            max_items=_positive_int(
                env.get("HYPRIAL_HOLD_MAX_ITEMS"), DEFAULT_HOLD_MAX_ITEMS
            ),
            status_retention_ms=_positive_int(
                env.get("HYPRIAL_STATUS_RETENTION_MS"), DEFAULT_STATUS_RETENTION_MS
            ),
        )


# Reasons written by the recipient itself, inside the transaction that
# committed the message to its own inbox.  Everything else is second-hand.
RECIPIENT_OBSERVED_REASONS = frozenset(
    {
        HoldReason.RECIPIENT_COMMIT.value,
        HoldReason.RECIPIENT_DUPLICATE.value,
    }
)


def _precedence(record: DeliveryStatus) -> tuple[int, int]:
    """Rank two claims about the same message.  Higher wins.

    Ranked by **who observed the outcome**, never by who wrote it down first.
    Competing records are written on different machines, so their
    ``recorded_at_ms`` values come off different clocks and comparing them
    across holders is precisely the mistake this design forbids everywhere
    else: ``HoldPolicy`` refuses to compute elapsed time from a foreign clock,
    and section 8.3 refuses to order anything by send time.

    This ranking used to be by timestamp, on the assumption that a mirrored
    verdict "can only be later" than the recipient's own.  It is measurably
    the other way round -- the mirror is stamped on the sending side around a
    blocking round trip, so it lands earlier -- and the authoritative record
    therefore lost every time.  The margin observed was 4ms, which is well
    inside ordinary clock skew between two hosts: even reversing the
    assumption would only have made the bug rarer, not absent.  So the clock
    is out of the decision entirely.

    Three tiers, strongest first:

    2. ``fetched`` observed by the recipient (``RECIPIENT_COMMIT``,
       ``RECIPIENT_DUPLICATE``) -- the message is in its inbox, committed in
       the same transaction as the record.
    1. ``fetched`` mirrored by a holder off a receipt -- true only if the
       receipt leg told the truth, and that leg is defect #4.  An unrecognised
       reason lands here too: ``reason`` is a free-form string, and an unknown
       claim must never be promoted to first-hand evidence.
    0. ``expired`` -- the holder observed only the *absence* of a receipt.

    The second element is a deterministic tie-break **within** a tier, not
    evidence: earliest among fetched (two recipient records for one message
    can only come from the one recipient, so that clock is self-consistent),
    latest among evictions (a holder's last word about its own copy).
    """

    if record.state is TerminalState.FETCHED:
        if record.reason in RECIPIENT_OBSERVED_REASONS:
            return (2, -record.recorded_at_ms)
        return (1, -record.recorded_at_ms)
    return (0, record.recorded_at_ms)


def conflicting_message_ids(records: Iterable[DeliveryStatus]) -> tuple[str, ...]:
    """Message ids whose holders do not agree on the terminal state.

    This is the only situation in which ``merge_delivery_status`` has to
    *choose*, and therefore the only situation in which an incomplete set of
    holders can produce a confidently wrong answer.  Reporting it costs one
    pass and turns "the merge silently picked one" into something a caller can
    see.
    """

    states: dict[str, set[str]] = {}
    for record in records:
        states.setdefault(record.message_id, set()).add(record.state.value)
    return tuple(sorted(key for key, values in states.items() if len(values) > 1))


def merge_delivery_status(
    records: Iterable[DeliveryStatus],
) -> tuple[DeliveryStatus, ...]:
    """Collapse records for the same message, newest first.

    ``fetched`` beats ``expired`` unconditionally.  The two legitimately
    coexist across nodes: when a recipient commits a message but its receipt
    never makes it back, the holder eventually evicts its copy and writes
    ``expired`` while the recipient holds ``fetched``.  The recipient observed
    possession directly; the holder only observed the absence of a receipt --
    over a leg known to lose them.  Direct observation wins.
    """

    best: dict[str, DeliveryStatus] = {}
    for record in records:
        current = best.get(record.message_id)
        if current is None or _precedence(record) > _precedence(current):
            best[record.message_id] = record
    return tuple(
        sorted(best.values(), key=lambda item: (-item.recorded_at_ms, item.message_id))
    )


_R = TypeVar("_R")


def _guarded(method: Callable[..., _R]) -> Callable[..., _R]:  # noqa: UP047
    @wraps(method)
    def locked(self: DeliveryStatusStore, *args: Any, **kwargs: Any) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class DeliveryStatusStore:
    """Terminal-state ledger, sharing the inbox connection on purpose.

    Sharing the connection is what makes the record and the state change it
    describes commit together.  ``record`` deliberately does **not** commit:
    callers invoke it inside the ``with connection:`` block that inserts the
    inbox row or deletes the held row, so a crash can never leave a message
    gone with no record of where it went.

    The lock is the inbox service's own re-entrant lock, for the same reason:
    reads arrive on a Zenoh callback thread while the delivery pump writes on
    another, and both drive one connection.  Re-entrancy makes the nesting
    inside already-locked service methods free.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        lock: AbstractContextManager[bool] | None = None,
        retention_ms: int = DEFAULT_STATUS_RETENTION_MS,
    ) -> None:
        self._db = connection
        self._lock = lock if lock is not None else threading.RLock()
        self.retention_ms = retention_ms
        self._create_schema()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS delivery_status (
                message_id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                state TEXT NOT NULL,
                holder TEXT NOT NULL,
                reason TEXT NOT NULL,
                conversation_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT,
                recorded_at_ms INTEGER NOT NULL,
                purge_after_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delivery_status_sender
                ON delivery_status(sender, recorded_at_ms);
            CREATE INDEX IF NOT EXISTS delivery_status_purge
                ON delivery_status(purge_after_ms);
            """
        )
        self._db.commit()

    def record(
        self,
        *,
        message_id: str,
        sender: str,
        recipient: str,
        state: TerminalState,
        holder: str,
        reason: str,
        now_ms: int,
        conversation_id: str = "",
        idempotency_key: str | None = None,
    ) -> DeliveryStatus:
        """Write a terminal record.  Caller owns the transaction."""

        self._db.execute(
            """INSERT INTO delivery_status (
                   message_id, sender, recipient, state, holder, reason,
                   conversation_id, idempotency_key, recorded_at_ms, purge_after_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(message_id) DO UPDATE SET
                   state = excluded.state,
                   holder = excluded.holder,
                   reason = excluded.reason,
                   recorded_at_ms = excluded.recorded_at_ms,
                   purge_after_ms = excluded.purge_after_ms
               WHERE delivery_status.state = ? AND excluded.state = ?""",
            (
                message_id,
                sender,
                recipient,
                state.value,
                holder,
                reason,
                conversation_id,
                idempotency_key,
                now_ms,
                now_ms + self.retention_ms,
                TerminalState.EXPIRED.value,
                TerminalState.FETCHED.value,
            ),
        )
        return DeliveryStatus(
            message_id=message_id,
            sender=sender,
            recipient=recipient,
            state=state,
            holder=holder,
            reason=reason,
            recorded_at_ms=now_ms,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )

    def purge(self, *, now_ms: int) -> int:
        cursor = self._db.execute(
            "DELETE FROM delivery_status WHERE purge_after_ms <= ?", (now_ms,)
        )
        return int(cursor.rowcount)

    @_guarded
    def lookup(self, message_id: str) -> DeliveryStatus | None:
        row = self._db.execute(
            "SELECT * FROM delivery_status WHERE message_id = ?", (message_id,)
        ).fetchone()
        return None if row is None else self._row(row)

    @_guarded
    def for_sender(
        self, sender: str, *, message_id: str | None = None, limit: int = 500
    ) -> tuple[DeliveryStatus, ...]:
        if message_id is not None:
            rows = self._db.execute(
                "SELECT * FROM delivery_status WHERE sender = ? AND message_id = ?",
                (sender, message_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT * FROM delivery_status WHERE sender = ?
                   ORDER BY recorded_at_ms DESC, message_id LIMIT ?""",
                (sender, limit),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    @_guarded
    def count(self) -> int:
        return int(
            self._db.execute("SELECT COUNT(*) FROM delivery_status").fetchone()[0]
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> DeliveryStatus:
        return DeliveryStatus(
            message_id=str(row["message_id"]),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]),
            state=TerminalState(str(row["state"])),
            holder=str(row["holder"]),
            reason=str(row["reason"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
            conversation_id=str(row["conversation_id"] or ""),
            idempotency_key=row["idempotency_key"],
        )


def encode_status_frame(
    records: Iterable[DeliveryStatus], *, holder: str | None = None
) -> bytes:
    value: dict[str, Any] = {
        "schema": STATUS_FRAME_SCHEMA,
        "records": [record.to_json() for record in records],
    }
    if holder is not None:
        value["holder"] = holder
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _status_frame_holder(frame: bytes) -> str | None:
    """Read additive query metadata without claiming the records decoded."""

    try:
        value = json.loads(frame)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    holder = value.get("holder") if isinstance(value, dict) else None
    return holder if isinstance(holder, str) and holder else None


def decode_status_frame(frame: bytes) -> tuple[DeliveryStatus, ...]:
    value = json.loads(frame)
    if value.get("schema") != STATUS_FRAME_SCHEMA:
        raise ValueError("unsupported delivery status frame schema")
    holder = value.get("holder")
    if holder is not None and (not isinstance(holder, str) or not holder):
        raise ValueError("delivery status frame holder must be a non-empty string")
    return tuple(DeliveryStatus.from_json(item) for item in value["records"])


def parse_status_selector(selector: str, keys: KeySpace) -> tuple[str, str | None]:
    """Split ``msg/status/<sender>[/<message_id>]`` into its identities."""

    key = selector.split("?", 1)[0]
    prefix = f"{keys.prefix}/msg/status/"
    if not key.startswith(prefix):
        raise ValueError(f"not a delivery-status key: {selector}")
    segments = [segment for segment in key[len(prefix) :].split("/") if segment]
    if not segments or len(segments) > 2:
        raise ValueError(f"not a delivery-status key: {selector}")
    sender = keys.decode_identity(segments[0])
    message_id = keys.decode_identity(segments[1]) if len(segments) == 2 else None
    return sender, message_id


@dataclass(frozen=True, slots=True)
class UndecodableReply:
    """One reply that arrived and could not be turned into records."""

    key: str
    detail: str
    holder: str | None = None


@dataclass(frozen=True, slots=True)
class HolderReplyCount:
    """How many separate replies arrived under one holder name in one pull.

    A count above one is the wire-side signature of a duplicated node
    identity: each daemon instance answers the status query under the same
    name, and name-keyed sets (``responded_holders``) collapse them back into
    one.  ``records`` counts the records those replies carried (0 for an
    undecodable reply whose envelope still named its holder).
    """

    holder: str
    replies: int
    records: int


@dataclass(frozen=True, slots=True)
class StatusQueryServed:
    """One status query as seen by the node answering it."""

    selector: str
    sender: str | None
    message_id: str | None
    records: int
    answered: bool


@dataclass(frozen=True, slots=True)
class StatusQueryReport:
    """What one pull actually collected -- not only what it concluded.

    ``merge_delivery_status`` resolves disagreement between holders, and the
    rule that resolves it (``fetched`` beats ``expired``) is sound only over
    the **complete** set of holders.  Holders disagree in exactly one
    situation: the recipient committed the message but its receipt was lost,
    so the holder evicted its copy.  Lose the recipient's record from that set
    and the merge answers ``expired`` about a message that was delivered --
    confidently, and with no trace that anything went missing.

    Nothing here can prove completeness: the number of holders on a mesh is
    not knowable, which is why the design makes the record re-pullable instead
    of pretending one pull is final.  What it does do is count every **known**
    way a reply can be lost, so ``no_known_loss`` distinguishes "this verdict
    came from everything that answered" from "something answered and was
    dropped on the floor".

    ``records`` is deliberately **unmerged**.  A report describes what was
    collected; collapsing it is a separate decision, and folding the two
    together is how the holder that lost a merge became invisible to the
    diagnostics that were supposed to notice it was missing.  Callers pass
    ``records`` through ``merge_delivery_status`` when they want a verdict.
    """

    key: str = ""
    records: tuple[DeliveryStatus, ...] = ()
    replies: int = 0
    decoded: int = 0
    responded_holders: tuple[str, ...] = ()
    holder_reply_counts: tuple[HolderReplyCount, ...] = ()
    undecodable: tuple[UndecodableReply, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def holders(self) -> tuple[str, ...]:
        """Every holder whose records survived decoding (legacy diagnostic)."""

        return tuple(sorted({record.holder for record in self.records}))

    @property
    def undecodable_holders(self) -> tuple[str, ...]:
        """Named holders whose reply arrived but could not be decoded."""

        return tuple(
            sorted({reply.holder for reply in self.undecodable if reply.holder})
        )

    @property
    def duplicate_holders(self) -> tuple[HolderReplyCount, ...]:
        """Holder names that answered this pull more than once.

        Legacy frames without envelope metadata are attributed per record
        holder (one reply per sample that mentions the name), so a relayed or
        merged legacy frame can under- or over-count; new frames name their
        answering node and count exactly.
        """

        return tuple(
            entry for entry in self.holder_reply_counts if entry.replies > 1
        )

    @property
    def no_known_loss(self) -> bool:
        """True when no reply was lost on the way in.  Not a completeness claim."""

        return not self.undecodable and not self.errors


class DeliveryStatusEndpoint:
    """Serves this node's terminal records on ``msg/status/**``.

    One endpoint per node, not per actor: the records are keyed by *sender*,
    and any node may hold the outcome of any sender's message.  Every valid
    query gets a frame naming this node, including an empty ``records`` frame.
    That positive response is what lets a caller distinguish "this recipient
    has no better record" from "this recipient did not answer".

    ``observer`` records what the node was asked and whether it emitted a
    response.  Invalid selectors still return ``None`` and are observed as
    unanswered.
    """

    def __init__(
        self,
        session: TransportSession,
        store: DeliveryStatusStore,
        *,
        holder: str | None = None,
        keys: KeySpace | None = None,
        observer: Callable[[StatusQueryServed], None] | None = None,
    ) -> None:
        self._store = store
        self._holder = holder
        self._keys = keys or KeySpace()
        self._observer = observer
        self._registrations: list[Registration] = [
            session.declare_queryable(self._keys.message_status_any(), self._answer)
        ]

    def _answer(self, selector: str) -> bytes | None:
        try:
            sender, message_id = parse_status_selector(selector, self._keys)
        except ValueError:
            self._observe(StatusQueryServed(selector, None, None, 0, answered=False))
            return None
        records = self._store.for_sender(sender, message_id=message_id)
        answered = bool(records) or self._holder is not None
        self._observe(
            StatusQueryServed(selector, sender, message_id, len(records), answered)
        )
        # An empty, named reply is evidence too: it distinguishes a recipient
        # that answered with no better record from a recipient that was absent.
        return (
            encode_status_frame(records, holder=self._holder) if answered else None
        )

    def _observe(self, served: StatusQueryServed) -> None:
        if self._observer is None:
            return
        try:
            self._observer(served)
        except Exception:  # noqa: BLE001 - telemetry must not break the reply
            pass

    def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()


def query_delivery_status(
    session: TransportSession,
    sender: str,
    *,
    message_id: str | None = None,
    keys: KeySpace | None = None,
    timeout: float = 2.0,
) -> StatusQueryReport:
    """Pull terminal records for ``sender`` from every holder on the mesh.

    The queried key must be **concrete**: a reply carries the query's own key
    expression, and a wildcard is not a valid reply key.  ``msg/status/<sender>``
    (all records) and ``msg/status/<sender>/<message_id>`` (one) are both
    concrete and both intersect the ``msg/status/**`` declaration.

    Returns a report rather than bare records.  The previous signature could
    not express the difference between "two holders agreed" and "one holder's
    reply was thrown away by the decode guard", and callers therefore had no
    way to know whether the verdict they got rested on a whole set.
    """

    space = keys or KeySpace()
    key = (
        space.message_status(sender, message_id)
        if message_id is not None
        else space.message_status_root(sender)
    )
    records: list[DeliveryStatus] = []
    responded_holders: set[str] = set()
    holder_replies: dict[str, int] = {}
    holder_record_counts: dict[str, int] = {}
    undecodable: list[UndecodableReply] = []
    errors: list[str] = []
    # `all_replies` is load-bearing, not a tuning knob.  Every holder answers
    # under the key that was queried, and Zenoh's default consolidation keeps
    # one reply per key -- so without this the pull returns a single holder's
    # verdict while reporting nothing lost, which is exactly the "confident
    # but wrong terminal state" this record exists to prevent.
    samples = session.get(key, timeout=timeout, errors=errors, all_replies=True)
    decoded = 0
    for sample in samples:
        holder = _status_frame_holder(sample.payload)
        try:
            decoded_records = decode_status_frame(sample.payload)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            # Was `continue`.  A holder's verdict vanished here without a
            # count, a log or a key -- so a set missing that holder looked
            # exactly like a set that never had one.
            undecodable.append(
                UndecodableReply(
                    key=sample.key,
                    detail=f"{type(error).__name__}: {error}",
                    holder=holder,
                )
            )
            # An undecodable reply still proves the name answered; a count
            # above one is what makes a duplicated identity visible here.
            if holder is not None:
                holder_replies[holder] = holder_replies.get(holder, 0) + 1
            continue
        records.extend(decoded_records)
        # New frames identify their answering node even when records is empty.
        # Old non-empty frames remain readable and provide a safe fallback;
        # old empty frames cannot establish which node answered.
        if holder is not None:
            responded_holders.add(holder)
            holder_replies[holder] = holder_replies.get(holder, 0) + 1
            holder_record_counts[holder] = (
                holder_record_counts.get(holder, 0) + len(decoded_records)
            )
        else:
            responded_holders.update(record.holder for record in decoded_records)
            # Legacy attribution is per record holder: one reply per sample
            # that mentions the name, records counted to their own holder.
            for name in sorted({r.holder for r in decoded_records}):
                holder_replies[name] = holder_replies.get(name, 0) + 1
            for record in decoded_records:
                holder_record_counts[record.holder] = (
                    holder_record_counts.get(record.holder, 0) + 1
                )
        decoded += 1
    return StatusQueryReport(
        key=key,
        records=tuple(records),
        replies=len(samples) + len(errors),
        decoded=decoded,
        responded_holders=tuple(sorted(responded_holders)),
        holder_reply_counts=tuple(
            HolderReplyCount(
                holder=name,
                replies=holder_replies[name],
                records=holder_record_counts.get(name, 0),
            )
            for name in sorted(holder_replies)
        ),
        undecodable=tuple(undecodable),
        errors=tuple(errors),
    )
