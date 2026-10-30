"""One composition of the batch authority, shared by every daemon edge.

Both the IPC surface and the in-process managed runtime must produce the same
schema, the same rendered text and the same spill file for one selection.
This module is that single composition; neither caller re-derives ordering,
truncation or spill behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.inbox.batch.contract import (
    DEFAULT_INLINE_LIMIT_BYTES,
    FORMAT,
    SCHEMA_VERSION,
    BatchItem,
    BatchSnapshot,
)
from hyprial.daemon.impl.inbox.batch.cursor import (
    CursorError,
    cursor_digest,
    decode_cursor,
)
from hyprial.daemon.impl.inbox.batch.render import (
    InlineBudget,
    render_full,
    render_spill,
)
from hyprial.daemon.impl.inbox.batch.snapshot import build_snapshot
from hyprial.daemon.impl.inbox.batch.spill import (
    SpillUnavailable,
    prune,
    publish,
)

__all__ = ["BatchView", "capture_batch", "item_json"]


@dataclass(frozen=True, slots=True)
class BatchView:
    """The wire response plus the classified items the runtime needs."""

    response: dict[str, Any]
    items: tuple[BatchItem, ...]

    @property
    def cursor(self) -> str:
        value = self.response.get("cursor")
        return value if isinstance(value, str) else ""

    @property
    def text(self) -> str:
        delivery = self.response.get("delivery")
        if isinstance(delivery, Mapping):
            text = delivery.get("text")
            if isinstance(text, str):
                return text
        return ""

    @property
    def message_ids(self) -> tuple[str, ...]:
        if self.items:
            return tuple(item.message_id for item in self.items)
        rows = self.response.get("items")
        if not isinstance(rows, list):
            return ()
        return tuple(
            str(row["messageId"])
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("messageId"), str)
        )


def item_json(item: BatchItem) -> dict[str, Any]:
    return {
        "messageId": item.message_id,
        "deliveryId": item.delivery_id,
        "conversationId": item.conversation_id,
        "from": item.sender,
        "to": item.recipient,
        "intent": item.intent,
        "kind": item.kind,
        "createdAtMs": item.created_at_ms,
        "summary": item.summary,
        "body": item.body,
        "bodyBytes": item.body_bytes,
        "origin": dict(item.origin),
        "class": item.cls,
        "expectedAction": item.expected_action,
    }


def _empty(actor: str, epoch: str, limit: int) -> dict[str, Any]:
    return {
        "ok": True,
        "schemaVersion": SCHEMA_VERSION,
        "format": FORMAT,
        "cursor": "",
        "daemonEpoch": epoch,
        "actor": actor,
        "itemCount": 0,
        "totalBodyBytes": 0,
        "inlineLimitBytes": limit,
        "items": [],
        "delivery": {
            "mode": "inline",
            "text": "",
            "spillPath": None,
            "spillBytes": None,
            "spillSha256": None,
        },
    }


def capture_batch(
    inbox: object,
    actor: str,
    *,
    keys: tuple[str, ...],
    cursor_token: str | None = None,
    after_token: str | None = None,
    limit_bytes: int = DEFAULT_INLINE_LIMIT_BYTES,
    actor_home: Path | None = None,
    epoch: str = "",
    include_notices: bool = True,
    mark_fetched: bool = True,
    _origin_resolver_for_test: Callable[
        [object, str], dict[str, object]
    ] | None = None,
    only: tuple[str, ...] | None = None,
) -> BatchView:
    """Capture, classify, render and (when needed) spill one batch."""

    key = b""
    reader = getattr(inbox, "batch_cursor_key", None)
    if callable(reader):
        key = bytes(reader())
    cursor = after = None
    after_digest = None
    try:
        if cursor_token is not None:
            cursor = decode_cursor(cursor_token, key, actor=actor)
        if after_token is not None:
            after = decode_cursor(after_token, key, actor=actor)
            after_digest = cursor_digest(after_token)
    except CursorError:
        raise
    selection = inbox.batch_snapshot(  # type: ignore[attr-defined]
        actor,
        keys=keys,
        cursor=cursor,
        after=after,
        after_digest=after_digest,
        include_notices=include_notices,
        only=only,
        **({"mark_fetched": False} if not mark_fetched else {}),
    )
    snapshot = build_snapshot(
        selection,
        key=key,
        _origin_resolver_for_test=_origin_resolver_for_test,
    )
    response = _render_enriched_response(
        snapshot,
        limit_bytes=limit_bytes,
        actor_home=actor_home,
        epoch=epoch,
    )
    return BatchView(response, snapshot.items)


def _render_response(
    snapshot: BatchSnapshot,
    *,
    limit_bytes: int,
    actor_home: Path | None,
    epoch: str,
) -> dict[str, Any]:
    return _render_enriched_response(
        snapshot,
        limit_bytes=limit_bytes,
        actor_home=actor_home,
        epoch=epoch,
    )


def _render_enriched_response(
    snapshot: BatchSnapshot,
    *,
    limit_bytes: int,
    actor_home: Path | None,
    epoch: str,
) -> dict[str, Any]:
    if not snapshot.items:
        return _empty(snapshot.actor, epoch, limit_bytes)
    items = [item_json(item) for item in snapshot.items]
    full = render_full(snapshot)
    delivery: dict[str, Any] = {
        "mode": "inline",
        "text": full,
        "spillPath": None,
        "spillBytes": None,
        "spillSha256": None,
    }
    if actor_home is not None:
        prune(actor_home)
    if not InlineBudget(limit_bytes).fits(full):
        if actor_home is None:
            raise SpillUnavailable("no private actor state root for the batch")
        spilled = publish(actor_home, snapshot.cursor, full)
        for row in items:
            row["body"] = None
        delivery = {
            "mode": "spill",
            "text": render_spill(
                snapshot,
                spill_path=spilled.path,
                spill_bytes=spilled.size_bytes,
                spill_sha256=spilled.sha256,
            ),
            "spillPath": str(spilled.path),
            "spillBytes": spilled.size_bytes,
            "spillSha256": spilled.sha256,
        }
    return {
        "ok": True,
        "schemaVersion": SCHEMA_VERSION,
        "format": FORMAT,
        "cursor": snapshot.cursor,
        "daemonEpoch": epoch,
        "actor": snapshot.actor,
        "itemCount": snapshot.item_count,
        "totalBodyBytes": snapshot.total_body_bytes,
        "inlineLimitBytes": limit_bytes,
        "items": items,
        "delivery": delivery,
    }
