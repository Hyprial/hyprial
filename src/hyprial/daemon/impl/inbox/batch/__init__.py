"""Daemon-owned inbox batch: contract, snapshot, renderer and spill.

This is the one presentation authority for a coalesced inbox batch.  It is a
read-only projection over live inbox rows: it never acknowledges, replies or
stores a second copy of a message.
"""

from hyprial.daemon.impl.inbox.batch.contract import (
    DEFAULT_INLINE_LIMIT_BYTES,
    FORMAT,
    MAX_INLINE_LIMIT_BYTES,
    MIN_INLINE_LIMIT_BYTES,
    SCHEMA_VERSION,
    BatchBounds,
    BatchCursor,
    BatchItem,
    BatchMember,
    BatchRow,
    BatchSelection,
    BatchSnapshot,
    classify,
    inline_limit_from_environment,
)
from hyprial.daemon.impl.inbox.batch.cursor import (
    CursorError,
    cursor_digest,
    cursor_key,
    decode_cursor,
    encode_cursor,
)
from hyprial.daemon.impl.inbox.batch.render import (
    InlineBudget,
    render_full,
    render_inline,
    render_scalar_notice,
    render_scalar_summary,
    render_spill,
)
from hyprial.daemon.impl.inbox.batch.service import (
    BatchView,
    capture_batch,
    item_json,
)
from hyprial.daemon.impl.inbox.batch.snapshot import build_snapshot
from hyprial.daemon.impl.inbox.batch.spill import (
    SpillFile,
    SpillUnavailable,
    prune,
    publish,
)

__all__ = [
    "DEFAULT_INLINE_LIMIT_BYTES",
    "FORMAT",
    "MAX_INLINE_LIMIT_BYTES",
    "MIN_INLINE_LIMIT_BYTES",
    "SCHEMA_VERSION",
    "BatchBounds",
    "BatchCursor",
    "BatchItem",
    "BatchMember",
    "BatchRow",
    "BatchSelection",
    "BatchSnapshot",
    "BatchView",
    "CursorError",
    "InlineBudget",
    "SpillFile",
    "SpillUnavailable",
    "build_snapshot",
    "capture_batch",
    "classify",
    "cursor_digest",
    "cursor_key",
    "decode_cursor",
    "encode_cursor",
    "inline_limit_from_environment",
    "item_json",
    "prune",
    "publish",
    "render_full",
    "render_inline",
    "render_scalar_notice",
    "render_scalar_summary",
    "render_spill",
]
