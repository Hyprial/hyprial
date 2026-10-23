"""Freeze one selection into an ordered, cursor-authenticated snapshot."""

from __future__ import annotations

import json
from collections.abc import Callable

from hyprial.daemon.impl.api import visible_message_origin
from hyprial.daemon.impl.inbox.batch.contract import (
    SOURCE_INBOX,
    SOURCE_NOTICE,
    BatchCursor,
    BatchItem,
    BatchMember,
    BatchRow,
    BatchSelection,
    BatchSnapshot,
    class_rank,
)
from hyprial.daemon.impl.inbox.batch.cursor import encode_cursor

__all__ = ["build_snapshot"]


def _ordered_rows(selection: BatchSelection) -> tuple[BatchRow, ...]:
    """``(class_rank, arrival_ordinal)``; ordinals are globally unique."""

    def key(pair: tuple[int, BatchRow]) -> tuple[int, int, str, str]:
        index, row = pair
        return (
            class_rank(BatchItem.from_row(row)),
            row.ordinal,
            row.source,
            row.message.message_id,
        )

    return tuple(row for _index, row in sorted(enumerate(selection.rows), key=key))


def _decoded_payload(payload: bytes) -> object:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def build_snapshot(
    selection: BatchSelection,
    *,
    key: bytes,
    _origin_resolver_for_test: Callable[
        [object, str], dict[str, object]
    ] | None = None,
) -> BatchSnapshot:
    """Classify, sort and authenticate one authority selection.

    The ordered member vector goes into the cursor, so a replay never
    reclassifies or resorts members and a held/link-blocked row can never join
    an old cursor later.  ``createdAtMs`` is displayed metadata only.
    """

    rows = _ordered_rows(selection)
    origin_resolver = _origin_resolver_for_test or visible_message_origin
    items = tuple(
        item.with_origin(
            dict(origin_resolver(_decoded_payload(item.payload), item.sender))
        )
        for item in (BatchItem.from_row(row) for row in rows)
    )
    members = tuple(
        BatchMember(
            source=SOURCE_NOTICE if row.source == SOURCE_NOTICE else SOURCE_INBOX,
            message_id=row.message.message_id,
            ordinal=row.ordinal,
        )
        for row in rows
    )
    cursor = BatchCursor(
        actor=selection.actor,
        bounds=selection.bounds,
        members=members,
        deferred=selection.deferred,
        predecessor_digest=selection.predecessor_digest,
    )
    return BatchSnapshot(
        actor=selection.actor,
        cursor=encode_cursor(cursor, key),
        items=items,
        bounds=selection.bounds,
        total_body_bytes=sum(item.body_bytes for item in items),
    )
