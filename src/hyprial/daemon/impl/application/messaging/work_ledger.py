"""Node-local proof ledger for guarded work-item owner actions."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.kernel import atomic_json_write

_LOCK = threading.Lock()
_VERSION = 1


# LAX(same-uid): the ledger is node-local and 0600 but not authenticated. Any process
# running as the daemon's OS user can append rows, exactly as it can rewrite every other
# daemon store; a MAC key would be readable by that same user, so none is used. The
# guarantee holds only where agents cannot reach the host state dir (e.g. smolvm workers).
class WorkLedgerError(ValueError):
    """The node-local work verification ledger cannot be trusted."""


class WorkVerificationLedger:
    """Append and query action/version proofs outside every OrgFS space."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def journal_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.append")

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as error:
            raise WorkLedgerError(f"cannot parse {self.path}: {error}") from error
        if not isinstance(value, dict) or value.get("version") != _VERSION:
            raise WorkLedgerError(f"cannot parse {self.path}: unsupported ledger version")
        rows = value.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise WorkLedgerError(f"cannot parse {self.path}: rows must be objects")
        return [dict(row) for row in rows]

    def rows(self) -> tuple[dict[str, Any], ...]:
        """Return a detached snapshot; missing state means no local proof."""

        with _LOCK:
            rows = self._read()
            try:
                text = self.journal_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return tuple(rows)
            except (OSError, UnicodeError) as error:
                raise WorkLedgerError(
                    f"cannot parse {self.journal_path}: {error}"
                ) from error
            if text and not text.endswith("\n"):
                raise WorkLedgerError(
                    f"cannot parse {self.journal_path}: incomplete record"
                )
            for line in text.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise WorkLedgerError(
                        f"cannot parse {self.journal_path}: {error}"
                    ) from error
                if (
                    not isinstance(value, dict)
                    or value.get("version") != _VERSION
                    or not isinstance(value.get("row"), dict)
                ):
                    raise WorkLedgerError(
                        f"cannot parse {self.journal_path}: invalid record"
                    )
                rows.append(dict(value["row"]))
            return tuple(rows)

    def append(self, row: Mapping[str, Any]) -> None:
        """Durably append one proof without rewriting accumulated history."""

        record = dict(row)
        with _LOCK:
            if not self.path.exists():
                atomic_json_write(
                    self.path,
                    {"version": _VERSION, "rows": [record]},
                )
                return
            payload = (
                json.dumps(
                    {"version": _VERSION, "row": record},
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
            )
            try:
                descriptor = os.open(
                    self.journal_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    offset = 0
                    while offset < len(payload):
                        offset += os.write(descriptor, payload[offset:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise WorkLedgerError(
                    f"cannot append {self.journal_path}: {error}"
                ) from error
