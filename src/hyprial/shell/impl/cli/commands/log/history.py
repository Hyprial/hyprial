"""Shared readers for the local structured-log history.

``hyprial log`` and ``hyprial message trajectory`` both project the same
persisted JSONL history; the envelope validation and reader live here once.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from datetime import UTC, datetime
from typing import Any
from hyprial.kernel import PRE_TRAJECTORY_ARCHIVE
import json
import re

from hyprial.shell.impl.cli.commands.common.support import JsonObject
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _entry_timestamp(entry: JsonObject) -> datetime | None:
    raw = entry.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_log_entry(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _entry_timestamp(value) is not None
        and value.get("level") in {"debug", "info", "warn", "error"}
        and isinstance(value.get("component"), str)
        and isinstance(value.get("event"), str)
    )


def _read_log_history() -> tuple[list[JsonObject], int]:
    services = get_services()
    entries: list[JsonObject] = []
    skipped = 0
    logs_dir = services._state_dir() / "logs"
    files = sorted(logs_dir.rglob("*")) if logs_dir.is_dir() else []
    for path in files:
        if logs_dir / PRE_TRAJECTORY_ARCHIVE in path.parents:
            continue
        if not path.is_file() or re.search(r"\.jsonl(?:\.\d+)?$", path.name) is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            skipped += 1
            continue
        for line in lines:
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not _is_log_entry(entry):
                skipped += 1
                continue
            entries.append(entry)
    entries.sort(
        key=lambda entry: _entry_timestamp(entry) or datetime.min.replace(tzinfo=UTC)
    )
    return entries, skipped
