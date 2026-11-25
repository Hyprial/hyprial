"""Bounded, read-only SDK labels for existing routes and observed candidates.

Names never grant routing eligibility or change config revisions. Enumeration
uses the configured App namespace, five pages and a six-second soft budget;
missing names and SDK failures preserve IDs without exposing diagnostics.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import identities


def read_chat_names(
    home: Path, state: Path, adapter: str, chat_ids: set[str]
) -> tuple[dict[str, str | None], str]:
    if not chat_ids:
        return {}, "complete"
    names: dict[str, str | None] = {}
    deadline = time.monotonic() + 6.0
    try:
        _, gateway = identities.configured_identity_gateway(
            home, state, adapter, http_timeout_seconds=2.0
        )
        token: str | None = None
        seen: set[str] = set()
        for _ in range(5):
            if time.monotonic() >= deadline:
                break
            page, token = gateway.list_chats_page(page_token=token)
            for chat in page:
                if chat.chat_id in chat_ids:
                    names[chat.chat_id] = chat.name
            if chat_ids.issubset(names):
                return names, "complete"
            if token is None:
                break
            if token in seen:
                break
            seen.add(token)
    except Exception:
        return names, "partial" if names else "unavailable"
    return names, "partial"
