"""Durable UIDVALIDITY epochs, quarantine, cursors, and DSN observations."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from ..contracts import MailDeliveryObservation, MailboxEpochV1, SmtpTransactionReceipt


def _epoch_id(mailbox_scope: str, folder: str, uidvalidity: int) -> str:
    return hashlib.sha256(f"{mailbox_scope}|{folder}|{uidvalidity}".encode()).hexdigest()


class MailStateStore:
    """SQLite single-writer state for mail cursor rollover and observations."""

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS mailbox_epochs (
                    epoch_id TEXT PRIMARY KEY,
                    mailbox_scope TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    previous_epoch_id TEXT,
                    state TEXT NOT NULL,
                    detected_at_ms INTEGER NOT NULL,
                    snapshot_highest_uid INTEGER,
                    scan_next_uid INTEGER NOT NULL,
                    scan_end_uid INTEGER,
                    tail_next_uid INTEGER NOT NULL,
                    old_pending_count INTEGER NOT NULL,
                    quarantine_count INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    UNIQUE(mailbox_scope, folder, uidvalidity)
                );
                CREATE TABLE IF NOT EXISTS mailbox_current (
                    mailbox_scope TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    epoch_id TEXT NOT NULL,
                    checkpoint_uid INTEGER NOT NULL,
                    PRIMARY KEY(mailbox_scope, folder)
                );
                CREATE TABLE IF NOT EXISTS mail_rollover_quarantine (
                    epoch_id TEXT NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT,
                    raw_source_ref TEXT NOT NULL,
                    decision TEXT,
                    PRIMARY KEY(epoch_id, uid)
                );
                CREATE TABLE IF NOT EXISTS mail_delivery_observations (
                    observation_id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    authenticated INTEGER NOT NULL,
                    source_message_id TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    evidence_digest TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS mail_outbound_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    outbound_message_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS mail_outbound_recipients (
                    transaction_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    address_key TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    observation_version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(transaction_id, recipient_id),
                    UNIQUE(transaction_id, address_key),
                    FOREIGN KEY(transaction_id) REFERENCES mail_outbound_transactions(transaction_id)
                );
                CREATE TABLE IF NOT EXISTS mail_receipt_inbox (
                    receipt_id TEXT PRIMARY KEY,
                    source_message_id TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    raw_bytes BLOB NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def initialize_epoch(
        self,
        *,
        mailbox_scope: str,
        folder: str,
        uidvalidity: int,
        checkpoint_uid: int,
        operation_id: str,
        now_ms: int,
    ) -> MailboxEpochV1:
        epoch = MailboxEpochV1(
            "hyprial.mail.mailbox-epoch/v1",
            _epoch_id(mailbox_scope, folder, uidvalidity),
            mailbox_scope,
            folder,
            uidvalidity,
            None,
            "active",
            now_ms,
            None,
            checkpoint_uid + 1,
            None,
            checkpoint_uid + 1,
            0,
            0,
            operation_id,
        )
        with self._lock, self._connection:
            self._insert_epoch(epoch)
            self._connection.execute(
                """INSERT INTO mailbox_current(mailbox_scope,folder,epoch_id,checkpoint_uid)
                   VALUES(?,?,?,?)
                   ON CONFLICT(mailbox_scope,folder) DO UPDATE SET
                     epoch_id=excluded.epoch_id, checkpoint_uid=excluded.checkpoint_uid""",
                (mailbox_scope, folder, epoch.epoch_id, checkpoint_uid),
            )
        return epoch

    def observe_uidvalidity(
        self,
        *,
        mailbox_scope: str,
        folder: str,
        uidvalidity: int,
        snapshot_highest_uid: int | None,
        operation_id: str,
        now_ms: int,
        old_pending_count: int = 0,
    ) -> MailboxEpochV1:
        with self._lock, self._connection:
            prior = self.load_epoch(mailbox_scope, folder)
            if prior is not None and prior.uidvalidity == uidvalidity:
                return prior
            boundary = snapshot_highest_uid
            epoch = MailboxEpochV1(
                "hyprial.mail.mailbox-epoch/v1",
                _epoch_id(mailbox_scope, folder, uidvalidity),
                mailbox_scope,
                folder,
                uidvalidity,
                prior.epoch_id if prior else None,
                "rollover-detected" if boundary is not None else "held",
                now_ms,
                boundary,
                1,
                boundary if boundary and boundary > 0 else None,
                (boundary + 1) if boundary is not None else 1,
                old_pending_count,
                0,
                operation_id,
            )
            self._insert_epoch(epoch)
            self._connection.execute(
                """INSERT INTO mailbox_current(mailbox_scope,folder,epoch_id,checkpoint_uid)
                   VALUES(?,?,?,0)
                   ON CONFLICT(mailbox_scope,folder) DO UPDATE SET
                     epoch_id=excluded.epoch_id, checkpoint_uid=0""",
                (mailbox_scope, folder, epoch.epoch_id),
            )
            return epoch

    def advance_active_checkpoint(
        self,
        *,
        mailbox_scope: str,
        folder: str,
        uidvalidity: int,
        uid: int,
    ) -> MailboxEpochV1:
        """Advance the authoritative normal-epoch cursor after durable custody."""

        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT c.checkpoint_uid,e.*
                     FROM mailbox_current c JOIN mailbox_epochs e
                       ON e.epoch_id=c.epoch_id
                    WHERE c.mailbox_scope=? AND c.folder=?""",
                (mailbox_scope, folder),
            ).fetchone()
            if row is None:
                raise ValueError("mail active epoch does not exist")
            epoch = self._decode_epoch(row)
            if epoch.state != "active" or epoch.uidvalidity != uidvalidity:
                raise ValueError("mail checkpoint does not match the active epoch")
            checkpoint_uid = int(row["checkpoint_uid"])
            if uid < checkpoint_uid:
                raise ValueError("mail checkpoint moved the active cursor backwards")
            if uid > checkpoint_uid:
                self._connection.execute(
                    """UPDATE mailbox_current SET checkpoint_uid=?
                         WHERE mailbox_scope=? AND folder=? AND epoch_id=?""",
                    (uid, mailbox_scope, folder, epoch.epoch_id),
                )
                self._connection.execute(
                    """UPDATE mailbox_epochs SET scan_next_uid=?,tail_next_uid=?
                         WHERE epoch_id=? AND state='active'""",
                    (uid + 1, uid + 1, epoch.epoch_id),
                )
                row = self._connection.execute(
                    "SELECT * FROM mailbox_epochs WHERE epoch_id=?",
                    (epoch.epoch_id,),
                ).fetchone()
        if row is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("mail active epoch disappeared")
        return self._decode_epoch(row)

    def quarantine(
        self, *, epoch_id: str, uid: int, message_id: str | None, raw_source_ref: str
    ) -> None:
        with self._lock, self._connection:
            before = self._connection.total_changes
            self._connection.execute(
                """INSERT OR IGNORE INTO mail_rollover_quarantine(
                       epoch_id,uid,message_id,raw_source_ref,decision
                   ) VALUES(?,?,?,?,NULL)""",
                (epoch_id, uid, message_id, raw_source_ref),
            )
            if self._connection.total_changes != before:
                self._connection.execute(
                    "UPDATE mailbox_epochs SET quarantine_count=quarantine_count+1 WHERE epoch_id=?",
                    (epoch_id,),
                )

    def advance_rollover(
        self,
        *,
        epoch_id: str,
        uid: int,
        message_id: str | None,
        raw_source_ref: str,
    ) -> MailboxEpochV1:
        """Quarantine one journaled rollover UID and advance its durable scan cursor."""

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM mailbox_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            if row is None:
                raise ValueError("mail rollover epoch does not exist")
            epoch = self._decode_epoch(row)
            if epoch.scan_end_uid is None or uid > epoch.scan_end_uid:
                raise ValueError("mail UID is outside the rollover scan boundary")
            if uid < epoch.scan_next_uid:
                raise ValueError("mail rollover checkpoint moved the scan cursor backwards")
            before = self._connection.total_changes
            self._connection.execute(
                """INSERT OR IGNORE INTO mail_rollover_quarantine(
                       epoch_id,uid,message_id,raw_source_ref,decision
                   ) VALUES(?,?,?,?,NULL)""",
                (epoch_id, uid, message_id, raw_source_ref),
            )
            inserted = self._connection.total_changes != before
            next_uid = uid + 1
            state = "reconciling"
            self._connection.execute(
                """UPDATE mailbox_epochs
                      SET scan_next_uid=?, state=?,
                          quarantine_count=quarantine_count+?
                    WHERE epoch_id=?""",
                (next_uid, state, int(inserted), epoch_id),
            )
            updated = self._connection.execute(
                "SELECT * FROM mailbox_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("mail rollover epoch disappeared")
        return self._decode_epoch(updated)

    def advance_rollover_tail(self, *, epoch_id: str, uid: int) -> MailboxEpochV1:
        """Advance the durable post-boundary tail without clearing quarantine."""

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM mailbox_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            if row is None:
                raise ValueError("mail rollover epoch does not exist")
            epoch = self._decode_epoch(row)
            if epoch.scan_end_uid is None or epoch.scan_next_uid <= epoch.scan_end_uid:
                raise ValueError("mail rollover boundary scan is not complete")
            if uid < epoch.tail_next_uid:
                raise ValueError("mail rollover tail cursor cannot move backwards")
            self._connection.execute(
                "UPDATE mailbox_epochs SET tail_next_uid=? WHERE epoch_id=?",
                (uid + 1, epoch_id),
            )
            updated = self._connection.execute(
                "SELECT * FROM mailbox_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the same transaction
            raise RuntimeError("mail rollover epoch disappeared")
        return self._decode_epoch(updated)

    def resolve_quarantine(
        self, *, epoch_id: str, uid: int, decision: str
    ) -> MailboxEpochV1:
        if decision not in ("new-message", "duplicate-of-old", "ignore"):
            raise ValueError("mail rollover quarantine decision is invalid")
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT decision FROM mail_rollover_quarantine
                   WHERE epoch_id=? AND uid=?""",
                (epoch_id, uid),
            ).fetchone()
            if row is None:
                raise ValueError("mail rollover quarantine row does not exist")
            if row[0] is not None and row[0] != decision:
                raise ValueError("mail rollover quarantine decision conflicts")
            if row[0] is None:
                self._connection.execute(
                    """UPDATE mail_rollover_quarantine SET decision=?
                       WHERE epoch_id=? AND uid=? AND decision IS NULL""",
                    (decision, epoch_id, uid),
                )
                self._connection.execute(
                    """UPDATE mailbox_epochs
                          SET quarantine_count=quarantine_count-1
                        WHERE epoch_id=? AND quarantine_count>0""",
                    (epoch_id,),
                )
            updated = self._connection.execute(
                "SELECT * FROM mailbox_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
        if updated is None:
            raise ValueError("mail rollover epoch does not exist")
        return self._decode_epoch(updated)

    def record_transaction(
        self,
        *,
        transaction_id: str,
        outbound_message_id: str,
        recipients: Mapping[str, str],
    ) -> None:
        if not transaction_id or not outbound_message_id or not recipients:
            raise ValueError("mail transaction identity and recipients are required")
        normalized = tuple(
            sorted((str(recipient_id), str(address).casefold()) for recipient_id, address in recipients.items())
        )
        if any(not recipient_id or not address for recipient_id, address in normalized):
            raise ValueError("mail transaction recipients must not be empty")
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT outbound_message_id FROM mail_outbound_transactions WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if existing is not None and existing[0] != outbound_message_id:
                raise ValueError("mail transaction replay changed outbound message id")
            if existing is not None:
                prior_recipients = tuple(
                    (str(row[0]), str(row[1]))
                    for row in self._connection.execute(
                        """SELECT recipient_id,address_key FROM mail_outbound_recipients
                           WHERE transaction_id=? ORDER BY recipient_id,address_key""",
                        (transaction_id,),
                    ).fetchall()
                )
                if prior_recipients != normalized:
                    raise ValueError("mail transaction replay changed recipients")
                return
            self._connection.execute(
                "INSERT OR IGNORE INTO mail_outbound_transactions VALUES(?,?)",
                (transaction_id, outbound_message_id),
            )
            for recipient_id, address in normalized:
                row = self._connection.execute(
                    """SELECT address_key FROM mail_outbound_recipients
                       WHERE transaction_id=? AND recipient_id=?""",
                    (transaction_id, recipient_id),
                ).fetchone()
                if row is not None and row[0] != address:
                    raise ValueError("mail transaction replay changed recipient")
                self._connection.execute(
                    """INSERT OR IGNORE INTO mail_outbound_recipients(
                           transaction_id,recipient_id,address_key,delivery_state
                       ) VALUES(?,?,?,'accepted')""",
                    (transaction_id, recipient_id, address),
                )

    def record_smtp_receipt(self, receipt: SmtpTransactionReceipt) -> None:
        recipients = {row.recipient_id: row.recipient_id for row in receipt.recipients}
        self.record_transaction(
            transaction_id=receipt.transaction_id,
            outbound_message_id=receipt.outbound_message_id,
            recipients=recipients,
        )
        with self._lock, self._connection:
            for row in receipt.recipients:
                if row.data_state == "accepted":
                    state = "accepted"
                elif row.data_state == "unknown":
                    state = "unknown"
                elif (
                    row.rcpt_state == "refused-retryable"
                    or row.data_state == "rejected-retryable"
                ):
                    state = "failed-retryable"
                else:
                    state = "failed-final"
                self._connection.execute(
                    """UPDATE mail_outbound_recipients SET delivery_state=?
                       WHERE transaction_id=? AND recipient_id=? AND observation_version=0""",
                    (state, receipt.transaction_id, row.recipient_id),
                )

    def stage_receipt(
        self, raw: bytes, *, source_message_id: str, observed_at_ms: int
    ) -> str:
        if not source_message_id or observed_at_ms < 0:
            raise ValueError("mail receipt source identity is invalid")
        evidence_digest = hashlib.sha256(raw).hexdigest()
        receipt_id = hashlib.sha256(
            f"{source_message_id}\0{evidence_digest}".encode()
        ).hexdigest()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR IGNORE INTO mail_receipt_inbox(
                       receipt_id,source_message_id,observed_at_ms,evidence_digest,raw_bytes,state
                   ) VALUES(?,?,?,?,?,'pending')""",
                (receipt_id, source_message_id, observed_at_ms, evidence_digest, raw),
            )
        return receipt_id

    def pending_receipts(self, *, limit: int) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 100:
            raise ValueError("mail receipt reconcile limit must be in 1..100")
        with self._lock:
            rows = self._connection.execute(
                """SELECT receipt_id,source_message_id,observed_at_ms,evidence_digest,raw_bytes
                     FROM mail_receipt_inbox WHERE state='pending'
                    ORDER BY observed_at_ms,receipt_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def lookup_delivery(
        self, *, transaction_id: str | None, outbound_message_id: str | None, address: str
    ) -> tuple[str, str] | None:
        with self._lock:
            if transaction_id:
                row = self._connection.execute(
                    """SELECT r.transaction_id,r.recipient_id
                         FROM mail_outbound_recipients r
                        WHERE r.transaction_id=? AND r.address_key=?""",
                    (transaction_id, address.casefold()),
                ).fetchone()
            elif outbound_message_id:
                row = self._connection.execute(
                    """SELECT r.transaction_id,r.recipient_id
                         FROM mail_outbound_recipients r
                         JOIN mail_outbound_transactions t
                           ON t.transaction_id=r.transaction_id
                        WHERE t.outbound_message_id=? AND r.address_key=?""",
                    (outbound_message_id, address.casefold()),
                ).fetchone()
            else:
                row = None
        return (str(row[0]), str(row[1])) if row is not None else None

    def settle_receipt(
        self,
        *,
        receipt_id: str,
        observation: MailDeliveryObservation | None,
        delivery_state: str | None,
        reason_code: str,
    ) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT state FROM mail_receipt_inbox WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("mail receipt inbox row does not exist")
            if row[0] != "pending":
                return
            inserted = False
            if observation is not None:
                before = self._connection.total_changes
                self._connection.execute(
                    """INSERT OR IGNORE INTO mail_delivery_observations
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        observation.observation_id,
                        observation.transaction_id,
                        observation.recipient_id,
                        observation.kind,
                        int(observation.authenticated),
                        observation.source_message_id,
                        observation.observed_at_ms,
                        observation.evidence_digest,
                    ),
                )
                inserted = self._connection.total_changes != before
            if inserted and observation is not None and delivery_state is not None:
                self._connection.execute(
                    """UPDATE mail_outbound_recipients
                          SET delivery_state=?, observation_version=observation_version+1
                        WHERE transaction_id=? AND recipient_id=?""",
                    (delivery_state, observation.transaction_id, observation.recipient_id),
                )
            self._connection.execute(
                "UPDATE mail_receipt_inbox SET state='settled',reason_code=? WHERE receipt_id=?",
                (reason_code, receipt_id),
            )

    def delivery_status(self, transaction_id: str, recipient_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT delivery_state FROM mail_outbound_recipients
                   WHERE transaction_id=? AND recipient_id=?""",
                (transaction_id, recipient_id),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def observation_count(self, transaction_id: str, recipient_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                """SELECT COUNT(*) FROM mail_delivery_observations
                   WHERE transaction_id=? AND recipient_id=?""",
                (transaction_id, recipient_id),
            ).fetchone()
        return int(row[0])

    def quarantine_row(self, epoch_id: str, uid: int) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT uid,message_id,decision,raw_source_ref
                   FROM mail_rollover_quarantine WHERE epoch_id=? AND uid=?""",
                (epoch_id, uid),
            ).fetchone()
        if row is None:
            return None
        return {
            "uid": row["uid"],
            "messageId": row["message_id"],
            "decision": row["decision"],
            "rawSourceRef": row["raw_source_ref"],
        }

    def load_epoch(self, mailbox_scope: str, folder: str) -> MailboxEpochV1 | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT e.* FROM mailbox_epochs e JOIN mailbox_current c
                     ON c.epoch_id=e.epoch_id
                   WHERE c.mailbox_scope=? AND c.folder=?""",
                (mailbox_scope, folder),
            ).fetchone()
        return self._decode_epoch(row) if row is not None else None

    def append_observation(self, observation: MailDeliveryObservation) -> bool:
        with self._lock, self._connection:
            before = self._connection.total_changes
            self._connection.execute(
                """INSERT OR IGNORE INTO mail_delivery_observations VALUES(?,?,?,?,?,?,?,?)""",
                (
                    observation.observation_id,
                    observation.transaction_id,
                    observation.recipient_id,
                    observation.kind,
                    int(observation.authenticated),
                    observation.source_message_id,
                    observation.observed_at_ms,
                    observation.evidence_digest,
                ),
            )
            return self._connection.total_changes != before

    def _insert_epoch(self, epoch: MailboxEpochV1) -> None:
        self._connection.execute(
            """INSERT OR IGNORE INTO mailbox_epochs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                epoch.epoch_id,
                epoch.mailbox_scope,
                epoch.folder,
                epoch.uidvalidity,
                epoch.previous_epoch_id,
                epoch.state,
                epoch.detected_at_ms,
                epoch.snapshot_highest_uid,
                epoch.scan_next_uid,
                epoch.scan_end_uid,
                epoch.tail_next_uid,
                epoch.old_pending_count,
                epoch.quarantine_count,
                epoch.operation_id,
            ),
        )

    @staticmethod
    def _decode_epoch(row: sqlite3.Row) -> MailboxEpochV1:
        return MailboxEpochV1(
            "hyprial.mail.mailbox-epoch/v1",
            row["epoch_id"],
            row["mailbox_scope"],
            row["folder"],
            row["uidvalidity"],
            row["previous_epoch_id"],
            row["state"],
            row["detected_at_ms"],
            row["snapshot_highest_uid"],
            row["scan_next_uid"],
            row["scan_end_uid"],
            row["tail_next_uid"],
            row["old_pending_count"],
            row["quarantine_count"],
            row["operation_id"],
        )


def capture_uidvalidity_rollover(
    value: dict[str, object], *, store: MailStateStore | None = None
) -> dict[str, object]:
    """Compatibility seam for the frozen common rollover contract."""

    mailbox_scope = str(value.get("mailboxScope", "compat-mailbox"))
    folder = str(value["mailbox"])
    uidvalidity = int(value["newUidValidity"])
    uid = int(value["uid"])
    if store is not None:
        epoch = store.observe_uidvalidity(
            mailbox_scope=mailbox_scope,
            folder=folder,
            uidvalidity=uidvalidity,
            snapshot_highest_uid=max(uid, int(value.get("snapshotHighestUid", uid))),
            old_pending_count=int(value.get("oldPendingCount", 0)),
            operation_id=str(value.get("operationId", f"compat:{folder}:{uidvalidity}")),
            now_ms=int(value.get("nowMs", 0)),
        )
        if epoch.scan_end_uid is not None and epoch.scan_next_uid <= uid <= epoch.scan_end_uid:
            store.advance_rollover(
                epoch_id=epoch.epoch_id,
                uid=uid,
                message_id=str(value["messageId"]) if value.get("messageId") is not None else None,
                raw_source_ref=str(value.get("rawSourceRef", f"compat:{uidvalidity}:{uid}")),
            )
        else:
            store.quarantine(
                epoch_id=epoch.epoch_id,
                uid=uid,
                message_id=str(value["messageId"]) if value.get("messageId") is not None else None,
                raw_source_ref=str(value.get("rawSourceRef", f"compat:{uidvalidity}:{uid}")),
            )
    return {
        "namespace": f"{folder}:{uidvalidity}",
        "quarantined": True,
        "checkpoint": uid,
        "deduplicatedByMessageId": False,
    }


__all__ = ["MailStateStore", "capture_uidvalidity_rollover"]
