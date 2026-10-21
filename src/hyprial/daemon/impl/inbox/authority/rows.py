"""Typed decoding of durable link ledger rows."""
import sqlite3
from hyprial.daemon.impl.inbox.contracts.api import LinkLedgerRow

def ledger_row(row: sqlite3.Row) -> LinkLedgerRow:
    return LinkLedgerRow(
        actor=str(row["actor"]),
        message_id=str(row["message_id"]),
        direction=str(row["direction"]),
        peer=str(row["peer"]),
        intent=str(row["intent"]),
        reply_to=(str(row["reply_to"]) if row["reply_to"] is not None else None),
        state=str(row["state"]),
        opened_at_ms=int(row["opened_at_ms"]),
        sent_at_ms=(
            int(row["sent_at_ms"]) if row["sent_at_ms"] is not None else None
        ),
        event_at_ms=(
            int(row["event_at_ms"]) if row["event_at_ms"] is not None else None
        ),
        threshold_ms=(
            int(row["threshold_ms"]) if row["threshold_ms"] is not None else None
        ),
        sample_pending=bool(row["sample_pending"]),
        expires_at_ms=int(row["expires_at_ms"]),
    )
