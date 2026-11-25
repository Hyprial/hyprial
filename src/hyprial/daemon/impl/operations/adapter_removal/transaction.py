"""Settle removal under the existing configuration and registry reservations.

The caller holds the configuration lease and runtime reservation. Credentials
are removed only after a durable commit; an interrupted prepared operation
compensates instead of replaying deletion. Recovery refuses changed preimages.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path

from hyprial.kernel import fsync_directory, write_private_file_atomic
from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterConfigConflictError
from .journal import AdapterRemovalJournal, RemovalIntent, RemovalPhase


class RemovalTransaction:
    def __init__(self, home: Path, journal: AdapterRemovalJournal):
        self.home = Path(home)
        self.journal = journal
        self.channels = self.home / "channels.json"

    def _credential(self, intent: RemovalIntent) -> Path:
        if intent.remove_credential and intent.credential_ref != f"lark-{intent.adapter}":
            raise AdapterConfigConflictError("credential ownership changed")
        if any(part in intent.credential_ref for part in ("/", "\\", "\0")):
            raise AdapterConfigConflictError("credential path changed")
        return self.home / "secrets" / f"{intent.credential_ref}.json"

    def _credential_matches(self, intent: RemovalIntent, *, absent_ok: bool = False) -> bool:
        path = self._credential(intent)
        if path.is_symlink():
            return False
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            return absent_ok or intent.credential_digest is None
        return hashlib.sha256(value).hexdigest() == intent.credential_digest

    def commit(self, intent: RemovalIntent, registry) -> None:
        if self.channels.read_bytes() != intent.channels_before or not self._credential_matches(intent):
            raise AdapterConfigConflictError("adapter configuration or credential changed")
        prepared = self.journal.prepare(intent)
        if prepared.phase is not RemovalPhase.PREPARED:
            raise AdapterConfigConflictError("removal operation has already advanced")
        try:
            write_private_file_atomic(self.channels, intent.channels_after)
            registry.commit()
            self.journal.advance(intent.operation_id, RemovalPhase.PREPARED, RemovalPhase.COMMITTED)
        except BaseException:
            if self.journal.get(intent.operation_id).phase is RemovalPhase.COMMITTED:
                return
            self.compensate(intent.operation_id, registry)
            raise

    def compensate(self, operation_id: str, registry) -> None:
        record = self.journal.get(operation_id)
        if record is None or record.phase not in (RemovalPhase.PREPARED, RemovalPhase.COMPENSATING):
            raise AdapterConfigConflictError("removal is not available for compensation")
        intent = record.intent
        current = self.channels.read_bytes()
        if current not in (intent.channels_before, intent.channels_after) or not self._credential_matches(intent):
            raise AdapterConfigConflictError("adapter configuration or credential changed during recovery")
        if record.phase is RemovalPhase.PREPARED:
            self.journal.advance(operation_id, RemovalPhase.PREPARED, RemovalPhase.COMPENSATING)
        if current != intent.channels_before:
            write_private_file_atomic(self.channels, intent.channels_before)
        registry.rollback()
        self.journal.advance(operation_id, RemovalPhase.COMPENSATING, RemovalPhase.COMPENSATED)

    def finish(self, operation_id: str, *, settle: Callable[[], object] | None = None) -> None:
        record = self.journal.get(operation_id)
        if record is None:
            raise AdapterConfigConflictError("removal receipt is missing")
        if record.phase is RemovalPhase.COMPLETED:
            return
        if record.phase is not RemovalPhase.COMMITTED:
            raise AdapterConfigConflictError("removal is not committed")
        intent = record.intent
        if self.channels.read_bytes() != intent.channels_after:
            raise AdapterConfigConflictError("adapter configuration changed before cleanup")
        if intent.remove_credential:
            if not self._credential_matches(intent, absent_ok=True):
                raise AdapterConfigConflictError("credential changed before cleanup")
            path = self._credential(intent)
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)
        if settle is not None:
            settle()
        self.journal.advance(operation_id, RemovalPhase.COMMITTED, RemovalPhase.COMPLETED)
