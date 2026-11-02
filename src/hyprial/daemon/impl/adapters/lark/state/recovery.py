"""Bounded recovery metadata and durable terminal custody for frozen submissions.

Pending custody is capped at 1024 rows / 16 MiB by the store. Select all small
ordering headers, not a page of bodies: an older item or equal-time parent must
remain reachable even when its native ID is beyond the replay body budget.
Terminal rows retain the exact frozen request and count against that capacity.
"""

import json
from dataclasses import asdict
from datetime import datetime

from .records import DEAD_LETTER_TEXT_LIMIT, MAX_DEAD_LETTERS, DeadLetter

PENDING_RECOVERY_MAX_FAILURES = 3


class _PendingRecoveryState:
    def pending_inbound_headers(self) -> tuple[tuple[str, str, str, str | None], ...]:
        with self._lock:
            rows = self._db.execute(
                """SELECT message_id,
                   json_extract(request, '$.request.provider_metadata.chatId') AS chat,
                   json_extract(request, '$.request.provider_metadata.createTime') AS created,
                   json_extract(request, '$.request.provider_metadata.replyTo') AS parent
                   FROM pending_inbound_submissions
                   WHERE adapter = ? AND dead_lettered = 0 AND json_valid(request)
                   ORDER BY message_id""", (self.adapter,),
            ).fetchall()
        return tuple((r["message_id"], r["chat"], r["created"], r["parent"]) for r in rows)

    def pending_inbound_dead_lettered(self, message_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT dead_lettered FROM pending_inbound_submissions"
                " WHERE adapter = ? AND message_id = ?", (self.adapter, message_id),
            ).fetchone()
        return row is not None and bool(row[0])

    def resume_pending_inbound(self, message_id: str) -> None:
        """Only explicit operator recovery re-arms terminal frozen custody."""
        with self._transaction() as db:
            db.execute(
                "UPDATE pending_inbound_submissions SET dead_lettered = 0, recovery_failures = 0"
                " WHERE adapter = ? AND message_id = ? AND dead_lettered = 1",
                (self.adapter, message_id),
            )

    def fail_pending_inbound(self, pending: tuple[str, str, str], *, now: datetime) -> int:
        """Count failed recovery sweeps and atomically preserve a terminal letter.

        Unknown acceptance is not a definitive refusal: retain the immutable
        request for explicit recovery, never mark it delivered or discard it.
        """
        message_id, event_id, frozen = pending
        try:
            request = json.loads(frozen)["request"]
            metadata = request["provider_metadata"]
            if not isinstance(request, dict) or not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
        except (KeyError, TypeError, ValueError):
            request, metadata = {}, {}
        letter = DeadLetter(
            message_id=message_id, event_id=event_id,
            chat_id=str(metadata.get("chatId") or ""),
            chat_type=str(metadata.get("chatType") or "unknown"),
            conversation_id=str(request.get("conversation_id") or ""),
            sender_id=str(metadata.get("senderId") or "unknown"),
            sender_type=str(metadata.get("senderType") or "unknown"),
            text=str(request.get("text") or "[Invalid frozen submission]")[:DEAD_LETTER_TEXT_LIMIT],
            reason="submission-retry-exhausted", detail="acceptance-unconfirmed",
            created_at=now.isoformat(timespec="seconds"),
            create_time=None if metadata.get("createTime") is None else str(metadata["createTime"]),
            reply_to=None if metadata.get("replyTo") is None else str(metadata["replyTo"]),
            message_type=str(metadata.get("messageType") or "text"),
        )
        with self._transaction() as db:
            row = db.execute(
                "UPDATE pending_inbound_submissions SET recovery_failures = recovery_failures + 1"
                " WHERE adapter = ? AND message_id = ? AND request = ? AND dead_lettered = 0"
                " RETURNING recovery_failures", (self.adapter, message_id, frozen),
            ).fetchone()
            failures = 0 if row is None else int(row[0])
            if failures >= PENDING_RECOVERY_MAX_FAILURES:
                values = {"adapter": self.adapter, **asdict(letter)}
                fields = ", ".join(values)
                params = ", ".join(f":{key}" for key in values)
                db.execute(f"INSERT OR REPLACE INTO dead_letters ({fields}) VALUES ({params})", values)
                self._prune(db, "dead_letters", MAX_DEAD_LETTERS)
                db.execute(
                    "UPDATE pending_inbound_submissions SET dead_lettered = 1"
                    " WHERE adapter = ? AND message_id = ?", (self.adapter, message_id),
                )
        return failures
