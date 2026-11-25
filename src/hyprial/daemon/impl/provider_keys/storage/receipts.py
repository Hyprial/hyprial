# Decision record (contract freeze r3 §3.4; Q4 local-only usage)
# - UsageReceipts live only in H/state/provider-keys/receipts.jsonl: never in OrgFS,
#   bundles or transfer. Directory 0700, file 0600, append-only, fsync per row.
# - The row is validated against exactly the ten §3.4 fields before any byte is
#   written; an extra field is refused, so a secret, DEK or payload cannot ride along.
#   Validation failures carry a fixed message and never echo the row.
# - The partition directory is opened with O_NOFOLLOW and the file through that
#   directory fd with O_NOFOLLOW: symlinked directories or files, non-regular files
#   and hard links are refused instead of followed. Both opens carry O_NONBLOCK, so a
#   FIFO or device at the log path never blocks open() and is refused by the type check.
# - Every local failure (I/O, symlink, foreign owner, odd file type) raises
#   ReceiptLogUnavailable, an OSError with a fixed message, never a store code: the
#   receipt log is local bookkeeping and callers must not confuse it with the owner
#   store. Callers decide (launch logs and continues; list reports lastUsedAt null).
# - A crash may leave a torn trailing row. An append that finds the file not ending in
#   "\n" first writes "\n", so the torn bytes stay an isolated unparseable line and the
#   new row stays whole. A failed write or fsync truncates the file back to its size
#   before the append, so this process never leaves a torn row of its own.
# - Appenders serialize on an exclusive flock of the log fd held from the size probe
#   through write, fsync and any rollback. Without it a rollback could truncate a row
#   another thread or process committed meanwhile (concurrent launches each append),
#   breaking append-only. Readers take no lock: they only skip unparseable lines.
# - Integers (at, dekVersion) are JSON-safe: 0/1 .. 2**53-1, on write and on read.
# - A refusal may lack key material (e.g. awaiting-key has no keyId), so keyId,
#   dekVersion and grantDigest are nullable, but an injection carries all three.
# - last_used() is a read-side projection for list; unparseable rows are skipped,
#   including a row nested deeply enough to make the JSON parser raise RecursionError.
"""Local append-only provider-key UsageReceipt log."""
from __future__ import annotations

import json
import re
import uuid
from os import PathLike
from pathlib import Path
from typing import Any

from hyprial.kernel import (
    PrivateAccessUnsupported,
    append_private_file,
    inspect_private_file,
    read_private_file,
    secure_private_directory,
)

RECEIPT_FIELDS = frozenset({
    "at", "owner", "keyId", "provider", "agent", "agentId", "deviceUid", "dekVersion", "grantDigest", "outcome",
})
INJECTED = "injected"
REFUSED_PREFIX = "refused:"
PARTITION = "provider-keys"
LOG_NAME = "receipts.jsonl"

_KEY_ID = re.compile(r"pk\.[a-z2-7]{26}")
_AGENT_ID = re.compile(r"a\.[a-z2-7]{26}")
_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REFUSAL = re.compile(r"refused:[A-Z][A-Z0-9_]{0,63}")
_TEXT_MAX = 256
MAX_SAFE_INTEGER = 2**53 - 1


class InvalidReceipt(ValueError):
    """The row is not exactly a §3.4 UsageReceipt; the message never echoes it."""

    def __init__(self) -> None:
        super().__init__("invalid usage receipt")


class ReceiptLogUnavailable(OSError):
    """The local receipt log cannot be used safely; the message is fixed."""

    def __init__(self) -> None:
        super().__init__("usage receipt log unavailable")


def _unavailable() -> ReceiptLogUnavailable:
    return ReceiptLogUnavailable()


def _is_int(value: Any, minimum: int) -> bool:
    return type(value) is int and minimum <= value <= MAX_SAFE_INTEGER


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _TEXT_MAX and value.isprintable()


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_agent(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_receipt(row: Any) -> dict[str, Any]:
    """Return a copy of `row` when it is exactly a valid UsageReceipt; else InvalidReceipt."""
    if type(row) is not dict or set(row) != RECEIPT_FIELDS:
        raise InvalidReceipt()
    outcome = row["outcome"]
    injected = outcome == INJECTED
    checks = (
        _is_int(row["at"], 0),
        _is_text(row["owner"]),
        _matches(_PROVIDER, row["provider"]),
        _is_agent(row["agent"]),
        _matches(_AGENT_ID, row["agentId"]),
        _is_text(row["deviceUid"]),
        injected or _matches(_REFUSAL, outcome),
        _matches(_KEY_ID, row["keyId"]) or (not injected and row["keyId"] is None),
        _is_int(row["dekVersion"], 1) or (not injected and row["dekVersion"] is None),
        _matches(_DIGEST, row["grantDigest"]) or (not injected and row["grantDigest"] is None),
    )
    if not all(checks):
        raise InvalidReceipt()
    return dict(row)


def _open_partition(state_dir: Path, *, create: bool) -> Path | None:
    directory = state_dir / PARTITION
    if not create and not directory.exists():
        return None
    return secure_private_directory(directory, create=create)


def _check_log(path: Path) -> None:
    inspect_private_file(path)


def append_receipt(state_dir: str | PathLike[str], row: dict[str, Any]) -> None:
    """Validate and durably append one UsageReceipt to H/state/provider-keys/receipts.jsonl."""
    checked = validate_receipt(row)
    encoded = (json.dumps(checked, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    try:
        directory = _open_partition(Path(state_dir), create=True)
        append_private_file(directory / LOG_NAME, encoded)
    except (OSError, PrivateAccessUnsupported):
        raise _unavailable() from None


def _read_log(state_dir: Path) -> bytes:
    try:
        directory = _open_partition(state_dir, create=False)
        if directory is None:
            return b""
        try:
            return read_private_file(directory / LOG_NAME)[0]
        except FileNotFoundError:
            return b""
    except (OSError, PrivateAccessUnsupported):
        raise _unavailable() from None


def last_used(state_dir: str | PathLike[str]) -> dict[str, int]:
    """Latest injection time per keyId from the local log; invalid rows are ignored."""
    latest: dict[str, int] = {}
    for line in _read_log(Path(state_dir)).splitlines():
        try:
            row = validate_receipt(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if row["outcome"] == INJECTED:
            key_id = row["keyId"]
            latest[key_id] = max(latest.get(key_id, row["at"]), row["at"])
    return latest
