"""Read-only Apple Messages database access and controlled AppleScript sending."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class IMessageRow:
    rowid: int
    message_guid: str
    chat_guid: str
    handle: str
    text: str
    is_from_me: bool
    date: int

    def __post_init__(self) -> None:
        if self.rowid < 1 or self.date < 0:
            raise ValueError("iMessage row identifiers and dates must be non-negative")
        for name in ("message_guid", "chat_guid", "handle", "text"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.is_from_me, bool):
            raise TypeError("is_from_me must be bool")


class SQLiteMessageReader:
    """Open ``chat.db`` with SQLite ``mode=ro`` and issue text-DM-only queries."""

    def __init__(
        self,
        path: str | Path,
        *,
        test_path_authority: Callable[[Path], bool] | None = None,
    ) -> None:
        resolved = Path(path).expanduser().resolve(strict=True)
        canonical = (Path.home() / "Library" / "Messages" / "chat.db").resolve(
            strict=False
        )
        if resolved != canonical and not (
            test_path_authority is not None and test_path_authority(resolved)
        ):
            raise ValueError(
                "iMessage database must be the canonical user Library/Messages/chat.db"
            )
        self.connection_uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"

    def read_after(self, rowid: int, *, limit: int) -> tuple[IMessageRow, ...]:
        if rowid < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid iMessage cursor or page size")
        connection = sqlite3.connect(self.connection_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                """
                SELECT m.ROWID AS rowid, m.guid AS message_guid,
                       c.guid AS chat_guid, h.id AS handle, m.text AS text,
                       m.is_from_me AS is_from_me, m.date AS date
                  FROM message m
                  JOIN chat_message_join cmj ON cmj.message_id=m.ROWID
                  JOIN chat c ON c.ROWID=cmj.chat_id
                  JOIN handle h ON h.ROWID=m.handle_id
                 WHERE m.ROWID > ?
                   AND m.text IS NOT NULL AND length(m.text) > 0
                   AND COALESCE(m.cache_has_attachments, 0)=0
                   AND c.chat_identifier=h.id
                   AND c.guid LIKE 'iMessage;-;%'
                 ORDER BY m.ROWID ASC LIMIT ?
                """,
                (rowid, limit),
            ).fetchall()
            return tuple(
                IMessageRow(
                    int(item["rowid"]),
                    str(item["message_guid"]),
                    str(item["chat_guid"]),
                    str(item["handle"]),
                    str(item["text"]),
                    bool(item["is_from_me"]),
                    max(0, int(item["date"])),
                )
                for item in rows
            )
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class IMessageSendObservation:
    state: str
    definitely_not_sent: bool
    reason_code: str

    def __post_init__(self) -> None:
        if self.state not in ("unknown", "failed-final"):
            raise ValueError("iMessage sends have only unknown or pre-submit failed outcomes")


_SEND_SCRIPT = """
on run argv
  if (count of argv) is not 2 then error "expected handle and body"
  set recipientHandle to item 1 of argv
  set messageBody to item 2 of argv
  tell application "Messages"
    set targetService to first service whose service type = iMessage
    set targetBuddy to buddy recipientHandle of targetService
    send messageBody to targetBuddy
  end tell
end run
""".strip()


class OsaScriptSender:
    """Invoke only the absolute system osascript binary with data in argv."""

    @staticmethod
    def _binary() -> str | None:
        return shutil.which("osascript", path=os.defpath)

    def __init__(self, runner: Callable[..., object] = subprocess.run) -> None:
        self._runner = runner

    def send_text(self, handle: str, text: str) -> IMessageSendObservation:
        if not handle or not text:
            return IMessageSendObservation(
                "failed-final", True, "IMESSAGE_TARGET_OR_TEXT_INVALID"
            )
        binary = self._binary()
        if binary is None:
            return IMessageSendObservation(
                "failed-final", True, "IMESSAGE_MACOS_REQUIRED"
            )
        try:
            completed = self._runner(
                [binary, "-e", _SEND_SCRIPT, handle, text],
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return IMessageSendObservation(
                "unknown", False, "IMESSAGE_APPLESCRIPT_TIMEOUT_UNKNOWN"
            )
        if getattr(completed, "returncode", 1) != 0:
            return IMessageSendObservation(
                "unknown", False, "IMESSAGE_APPLESCRIPT_OUTCOME_UNKNOWN"
            )
        return IMessageSendObservation(
            "unknown", False, "IMESSAGE_RECEIPT_UNAVAILABLE"
        )


__all__ = [
    "IMessageRow",
    "IMessageSendObservation",
    "OsaScriptSender",
    "SQLiteMessageReader",
]
