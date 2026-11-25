"""Append-only private audit, bounded rotation; failure prevents sign delivery."""
from __future__ import annotations

import json
import os

from hyprial.identity import UserKey
from hyprial.kernel import append_private_file, ipc_errors

MAX_BYTES = 16 * 1024 * 1024
KEEP = 4


class Audit:
    def __init__(self, store):
        self.store = store

    def append(self, row: dict) -> None:
        encoded = (json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n").encode()
        try:
            with self.store.locked():
                path = self.store.root / "audit.jsonl"
                self.store.check_file(path, absent=True)
                if path.exists() and path.stat().st_size + len(encoded) > MAX_BYTES:
                    backups = [path.with_name(f"audit.jsonl.{n}") for n in range(1, KEEP + 1)]
                    for backup in backups:
                        self.store.check_file(backup, absent=True)
                    backups[-1].unlink(missing_ok=True)
                    for source, target in reversed(list(zip(backups, backups[1:]))):
                        if source.exists():
                            os.replace(source, target)
                    os.replace(path, backups[0])
                append_private_file(path, encoded)
                self.store.check_file(path)
        except (OSError, ValueError):
            raise UserKey.Error(ipc_errors.USER_KEY_QUARANTINED) from None
