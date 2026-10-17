"""WP7 §2.4: the reply-reminder text, bounded in UTF-8 bytes."""

from __future__ import annotations

from collections.abc import Sequence

HINT = "；回复用 reply，无需回复用 ack"


def reply_reminder_text(ids: Sequence[str], *, max_bytes: int) -> str:
    """Render ``"{N} 条请求未回复：{ids}；回复用 reply，无需回复用 ack"``.

    The action hint is reserved first, then the truncation marker, so a
    shortened list still says how many are omitted and what to do.  Ids are
    taken in arrival order until the next one would exceed ``max_bytes``; the
    full list always travels in the notice payload's ``openRequests``.
    """

    ids = tuple(ids)
    count = len(ids)
    prefix = f"{count} 条请求未回复："
    full = prefix + ", ".join(ids) + HINT
    if len(full.encode("utf-8")) <= max_bytes:
        return full

    marker = f" 等（共 {count} 条）"
    tail = marker + HINT
    kept: list[str] = []
    for item in ids:
        candidate = ", ".join((*kept, item))
        if len((prefix + candidate + tail).encode("utf-8")) <= max_bytes:
            kept.append(item)
        else:
            break
    return prefix + ", ".join(kept) + tail


__all__ = ["HINT", "reply_reminder_text"]
