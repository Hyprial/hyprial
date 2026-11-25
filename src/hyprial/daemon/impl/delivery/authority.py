"""The delivery body authority: bindings, interaction reads and hydration.

Decision notes (phase A, S5; merged plan §2.0 "Body hydration"):

- ``DeliveryBodies`` is held by the inbox authority facade, which passes the
  engine's row decoder and ``message_class_of``.  It owns no connection: the
  facade lends it a read-only connection per call.
- ``bind_body_retention`` binds the ``BodyResolver`` (S6
  ``ConversationBodies``) and the retention source (S7a's
  ``delivery.bodyRetentionMs`` snapshot) independently; an argument left
  out keeps its current binding, and None clears it.
- The retention source takes one positional argument, the grant's subject
  (integration I1).  A source that cannot be called that way is refused at
  bind time with ``TypeError``, not left to fall back to the default on
  every grant; the earlier binding is kept.
- Hydration runs inside the authority, before read admission: a dropped row
  is returned with the resolver's payload only when that payload hashes to
  the stored ``body_payload_sha256``.  With no drops (no grants) nothing is
  ever hydrated and every read is unchanged; with no resolver a dropped row
  stays residue, and ``retained_body`` answers None so ``message.read_body``
  raises its existing ``MESSAGE_BODY_UNAVAILABLE``.
- Interaction reads split by class through the injected accessor only and
  skip dropped rows, which a record already holds.

Decision notes (plan slice S1, rulings R4, R6, D5):

- The feed hands out every ``request`` and ``reply`` row of both classes,
  each with its class; the class no longer decides whether a row reaches
  the recorder.  The intent filter stays: it is the filter the history
  reads use, and every row it leaves out is a daemon notice from a
  ``system:hyprial:<node>`` sender, which has no peer actor and stays
  unrecorded (D5).
"""

from __future__ import annotations

import inspect
import sqlite3
import threading
from collections.abc import Callable

from hyprial.daemon.impl.delivery.bodies import payload_sha256
from hyprial.daemon.impl.delivery.ports import (
    BODY_SOURCES,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    BodyGrant,
    BodyRef,
    BodyResolver,
    ClassOf,
    EnvelopeLike,
    InteractionPage,
    InteractionRow,
    RetentionSource,
    RowDecoder,
    clamp_body_retention_ms,
)
from hyprial.daemon.impl.delivery.retention import POSITION_COLUMN, row_select

INTERACTION_PAGE_MAX = 256
HISTORY_SOURCE_BY_RANK = {0: SOURCE_INBOX, 2: SOURCE_SENDER_COPY}
_UNSET = object()

Hydrator = Callable[[sqlite3.Row, EnvelopeLike], EnvelopeLike]


def _require_retention_source(source: object) -> None:
    if source is None:
        return
    if not callable(source):
        raise TypeError("retention source must be callable as source(subject)")
    try:
        signature = inspect.signature(source)
    except (TypeError, ValueError):
        return
    try:
        signature.bind("agent:subject")
    except TypeError as error:
        raise TypeError(
            "retention source must be callable as source(subject)"
        ) from error


def _column(row: sqlite3.Row, name: str) -> object:
    return row[name] if name in row.keys() else None


class DeliveryBodies:
    """Body release bindings and the hydrating reads of the inbox authority."""

    def __init__(self, *, decode: RowDecoder, class_of: ClassOf) -> None:
        self._decode = decode
        self._class_of = class_of
        self._lock = threading.Lock()
        self._resolver: BodyResolver | None = None
        self._retention: RetentionSource | None = None

    def bind_body_retention(
        self,
        *,
        resolver: BodyResolver | None | object = _UNSET,
        retention_ms: RetentionSource | None | object = _UNSET,
    ) -> None:
        """Bind the body resolver and/or the retention source."""

        if retention_ms is not _UNSET:
            _require_retention_source(retention_ms)
        with self._lock:
            if resolver is not _UNSET:
                self._resolver = resolver  # type: ignore[assignment]
            if retention_ms is not _UNSET:
                self._retention = retention_ms  # type: ignore[assignment]

    @property
    def resolver(self) -> BodyResolver | None:
        return self._resolver

    def needs_subject(self, grant: BodyGrant) -> bool:
        """Whether resolving *grant* asks the bound source, so needs its row."""

        return grant.retention_ms is None and self._retention is not None

    def retention_ms(self, grant: BodyGrant, subject: str | None = None) -> int:
        """The clamped retention for *grant*: its own, else the source for *subject*."""

        if grant.retention_ms is not None:
            return clamp_body_retention_ms(grant.retention_ms)
        source = self._retention
        if source is None or not isinstance(subject, str) or not subject:
            return clamp_body_retention_ms(None)
        try:
            return clamp_body_retention_ms(source(subject))
        except Exception:  # noqa: BLE001 - a broken source keeps the code default
            return clamp_body_retention_ms(None)

    def hydrate(
        self, source: str, row: sqlite3.Row, message: EnvelopeLike
    ) -> EnvelopeLike | None:
        """*message* with its original body, or None if a drop cannot be undone."""

        if _column(row, "body_dropped_at_ms") is None:
            return message
        resolver = self._resolver
        digest = _column(row, "body_payload_sha256")
        if resolver is None or not isinstance(digest, str):
            return None
        ref = BodyRef(
            source=source,
            position=int(row["body_position"]),
            message_id=message.message_id,
            payload_sha256=digest,
            ref=str(_column(row, "body_ref") or ""),
        )
        try:
            body = resolver.resolve(ref)
        except Exception:  # noqa: BLE001 - an unreadable record keeps the residue
            return None
        if not isinstance(body, bytes) or payload_sha256(body) != digest:
            return None
        return message.with_payload(body)

    def history_hydrator(self) -> Hydrator | None:
        """A history row hydrator, or None while no resolver is bound."""

        if self._resolver is None:
            return None

        def hydrate(row: sqlite3.Row, message: EnvelopeLike) -> EnvelopeLike:
            source = HISTORY_SOURCE_BY_RANK.get(int(row["source_rank"]))
            if source is None:
                return message
            return self.hydrate(source, row, message) or message

        return hydrate

    def retained_body(
        self, connection: sqlite3.Connection, message_id: str
    ) -> EnvelopeLike | None:
        """The retained inbox row of *message_id* with its original body."""

        row = connection.execute(
            f"{row_select(SOURCE_INBOX)} WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return self.hydrate(SOURCE_INBOX, row, self._decode(row))

    def _interaction(self, source: str, row: sqlite3.Row) -> InteractionRow | None:
        if row["intent"] not in ("request", "reply"):
            return None
        if _column(row, "body_dropped_at_ms") is not None:
            return None
        message = self._decode(row)
        return InteractionRow(
            source=source,
            position=int(row["body_position"]),
            message=message,
            payload_sha256=payload_sha256(message.payload),
            message_class=self._class_of(message),
        )

    def interaction_rows_after(
        self,
        connection: sqlite3.Connection,
        source: str,
        after: int,
        *,
        limit: int = 64,
    ) -> InteractionPage:
        """Conversational rows of *source* past position *after*, oldest first."""

        if source not in BODY_SOURCES:
            raise ValueError(f"source must be one of {sorted(BODY_SOURCES)}")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= INTERACTION_PAGE_MAX
        ):
            raise ValueError(f"limit must be an integer in 1..{INTERACTION_PAGE_MAX}")
        position = POSITION_COLUMN[source]
        rows = connection.execute(
            f"{row_select(source)} WHERE {position} > ? ORDER BY {position} LIMIT ?",
            (after, limit),
        ).fetchall()
        cursor = int(rows[-1]["body_position"]) if rows else after
        found = (self._interaction(source, row) for row in rows)
        return InteractionPage(
            rows=tuple(item for item in found if item is not None), cursor=cursor
        )

    def interaction_row_at(
        self, connection: sqlite3.Connection, source: str, position: int
    ) -> InteractionRow | None:
        """The conversational row at one position, or None."""

        if source not in BODY_SOURCES:
            raise ValueError(f"source must be one of {sorted(BODY_SOURCES)}")
        row = connection.execute(
            f"{row_select(source)} WHERE {POSITION_COLUMN[source]} = ?",
            (position,),
        ).fetchone()
        return None if row is None else self._interaction(source, row)


__all__ = [
    "HISTORY_SOURCE_BY_RANK",
    "INTERACTION_PAGE_MAX",
    "DeliveryBodies",
    "Hydrator",
]
