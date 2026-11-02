"""Notice lifecycle, owned by the inbox writer.

Retention uses the local receive clock, never the sender's timestamp. Legacy
rows retain NULL receive times and are bounded by the row cap. Pruning is not
acknowledgement. Ack tombstones have the same bounds: after eviction, replay
can be accepted again. Unknown/ambiguous legacy identities remain quarantined;
only a confirmed local agent mapping can change their recipient.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from hyprial.daemon.impl.inbox.contracts.api import AckResult, InboxMessage
from hyprial.daemon.impl.inbox.service.synchronization import (
    _record_committed_change,
    _synchronized,
)
from hyprial.kernel import ipc_errors, parse_agent_uri

SYSTEM_NOTICE_RETENTION_MS = 90 * 86_400_000
SYSTEM_NOTICE_MAX_ROWS = 10_000


def system_notice_counts(
    connection: sqlite3.Connection, node_id: str
) -> dict[str, int]:
    """Count retained rows, not live actors; bare non-node keys are quarantined."""
    row = connection.execute(
        """SELECT COUNT(*), COALESCE(SUM(CASE
            WHEN instr(recipient, ':') = 0 AND recipient != ? THEN 1 ELSE 0 END), 0)
            FROM system_notices""",
        (node_id,),
    ).fetchone()
    return {"pending": int(row[0]) - int(row[1]), "quarantined": int(row[1])}


class InboxServiceSystemNoticesMixin:
    @_synchronized
    def system_notice_counts(self, node_id: str) -> dict[str, int]:
        return system_notice_counts(self._db, node_id)

    def _initialize_system_notices(self) -> None:
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS system_notice_acks (
                message_id TEXT PRIMARY KEY, recipient TEXT NOT NULL,
                acked_at_ms INTEGER NOT NULL)""")
            self._db.execute("""CREATE INDEX IF NOT EXISTS system_notice_acks_acked_at
                ON system_notice_acks(acked_at_ms)""")
            columns = {
                row["name"]
                for row in self._db.execute("PRAGMA table_info(system_notices)")
            }
            if "received_at_ms" not in columns:
                self._db.execute(
                    "ALTER TABLE system_notices ADD COLUMN received_at_ms INTEGER"
                )
        self.migrate_system_notice_recipients()
        self.prune_system_notices()

    def _resolve_system_notice_recipient(self, recipient: str) -> str:
        resolver = self._notice_recipient_resolver
        if resolver is None or ":" in recipient or recipient == self.node_id:
            return recipient
        try:
            resolved = resolver(recipient)
        except Exception as error:
            if getattr(error, "code", None) == ipc_errors.AMBIGUOUS_TARGET:
                return recipient
            raise
        parsed = parse_agent_uri(resolved) if isinstance(resolved, str) else None
        return (
            resolved if parsed is not None and parsed[1] == self.node_id else recipient
        )

    def _canonicalize_system_notice(self, notice: InboxMessage) -> InboxMessage:
        try:
            recipient = self._resolve_system_notice_recipient(notice.recipient)
        except Exception as error:
            # Preserve the unresolved offer. Startup reconciliation must still
            # check canonical tombstones when the identity source recovers.
            self._logger.log(
                "warn",
                "system_notices.recipient_resolve_failed",
                errorType=type(error).__name__,
            )
            return notice
        return (
            replace(notice, recipient=recipient)
            if recipient != notice.recipient
            else notice
        )

    def _notice_acked_locked(self, recipient: str, message_id: str) -> bool:
        return (
            self._db.execute(
                """SELECT 1 FROM system_notice_acks WHERE recipient = ? AND message_id = ?
                AND acked_at_ms >= ?""",
                (recipient, message_id, self._now_ms() - SYSTEM_NOTICE_RETENTION_MS),
            ).fetchone()
            is not None
        )

    def _record_notice_ack_locked(
        self, recipient: str, message_id: str, now_ms: int
    ) -> None:
        self._db.execute(
            """INSERT INTO system_notice_acks(message_id, recipient, acked_at_ms)
                VALUES (?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET
                recipient = excluded.recipient, acked_at_ms = excluded.acked_at_ms""",
            (message_id, recipient, now_ms),
        )

    def _ack_system_notice(
        self, recipient: str, message_id: str, now_ms: int
    ) -> AckResult | None:
        # Called within ack's transaction; deletion and tombstone commit together.
        removed = self._db.execute(
            "DELETE FROM system_notices WHERE recipient = ? AND message_id = ?",
            (recipient, message_id),
        ).rowcount
        if removed:
            self._record_notice_ack_locked(recipient, message_id, now_ms)
            self._prune_system_notices_locked(now_ms=now_ms)
            return AckResult(message_id, True)
        if self._notice_acked_locked(recipient, message_id):
            return AckResult(message_id, True, "MESSAGE_ALREADY_SETTLED")
        return None

    @_synchronized
    def migrate_system_notice_recipients(self) -> tuple[int, int]:
        resolver = self._notice_recipient_resolver
        if resolver is None:
            return (0, 0)
        migrated = quarantined = 0
        try:
            with self._db:
                recipients = [
                    str(row[0])
                    for row in self._db.execute(
                        "SELECT DISTINCT recipient FROM system_notices ORDER BY recipient"
                    )
                ]
                for recipient in recipients:
                    if ":" in recipient or recipient == self.node_id:
                        continue
                    resolved = self._resolve_system_notice_recipient(recipient)
                    if resolved != recipient:
                        # Reconcile rows written by older/unresolved producers
                        # before moving them into the canonical inbox. Neither
                        # expire nor renew the matching acknowledgement here.
                        self._db.execute(
                            """DELETE FROM system_notices WHERE recipient = ?
                                AND EXISTS (SELECT 1 FROM system_notice_acks AS ack
                                    WHERE ack.message_id = system_notices.message_id
                                    AND ack.recipient = ? AND ack.acked_at_ms >= ?)""",
                            (
                                recipient,
                                resolved,
                                self._now_ms() - SYSTEM_NOTICE_RETENTION_MS,
                            ),
                        )
                        self._db.execute(
                            "UPDATE system_notices SET recipient = ? WHERE recipient = ?",
                            (resolved, recipient),
                        )
                        migrated += 1
                    else:
                        quarantined += 1
        except Exception as error:
            # Source failures roll back ALL mappings. Retry at the next startup.
            self._logger.log(
                "warn",
                "system_notices.recipients_migrate_failed",
                errorType=type(error).__name__,
            )
            return (0, 0)
        _record_committed_change(self, migrated > 0)
        if migrated or quarantined:
            self._logger.log(
                "info",
                "system_notices.recipients_migrated",
                migrated=migrated,
                quarantined=quarantined,
            )
        return (migrated, quarantined)

    @_synchronized
    def prune_system_notices(
        self,
        *,
        now_ms: int | None = None,
        retention_ms: int | None = None,
        max_rows: int | None = None,
    ) -> tuple[int, int]:
        with self._db:
            counts = self._prune_system_notices_locked(
                now_ms=now_ms,
                retention_ms=retention_ms,
                max_rows=max_rows,
            )
        _record_committed_change(self, any(counts))
        return counts

    def _prune_system_notices_locked(
        self,
        *,
        now_ms: int | None = None,
        retention_ms: int | None = None,
        max_rows: int | None = None,
        keep_message_id: str | None = None,
    ) -> tuple[int, int]:
        now = self._now_ms() if now_ms is None else now_ms
        retention = SYSTEM_NOTICE_RETENTION_MS if retention_ms is None else retention_ms
        limit = SYSTEM_NOTICE_MAX_ROWS if max_rows is None else max_rows
        cutoff = now - retention
        expired = self._db.execute(
            """DELETE FROM system_notices WHERE received_at_ms < ?
                AND message_id != ?""",
            (cutoff, keep_message_id or ""),
        ).rowcount
        # NULL receive times sort first; sender-controlled time never controls eviction.
        overflow = self._db.execute(
            """DELETE FROM system_notices WHERE message_id IN (
                SELECT message_id FROM system_notices WHERE message_id != ?
                ORDER BY received_at_ms, rowid LIMIT MAX(0,
                    (SELECT COUNT(*) FROM system_notices) - ?))""",
            (keep_message_id or "", limit),
        ).rowcount
        tombstone_expired = self._db.execute(
            "DELETE FROM system_notice_acks WHERE acked_at_ms < ?",
            (cutoff,),
        ).rowcount
        tombstone_overflow = self._db.execute(
            """DELETE FROM system_notice_acks WHERE message_id IN (
                SELECT message_id FROM system_notice_acks ORDER BY acked_at_ms, rowid
                LIMIT MAX(0, (SELECT COUNT(*) FROM system_notice_acks) - ?))""",
            (limit,),
        ).rowcount
        if expired or overflow or tombstone_expired or tombstone_overflow:
            self._logger.log(
                "info",
                "system_notices.pruned",
                expired=expired,
                overflow=overflow,
                tombstoneExpired=tombstone_expired,
                tombstoneOverflow=tombstone_overflow,
                retentionMs=retention,
                maxRows=limit,
            )
        return expired, overflow
