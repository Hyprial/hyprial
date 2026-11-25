"""Acknowledge rows addressed to ``user:<owner>`` once they are recorded (UA2).

Decision notes (UA2, 2026-11-18):

- A message to the owner is queued to this node's user agent like any
  other message, and the conversation record is its durable copy: the GUI
  reads the record, nothing reads the queue row.  Once the recorder has
  written and read back a row intact, this observer acknowledges it through
  the sanctioned ``message.ack`` request as the owner, so the row is not
  left to expire into dead-letter with a fail-loud alarm after the hold TTL.
- Only rows recorded intact are acknowledged.  A row the recorder skipped,
  could not record, or recorded as a placeholder (S2: refused or too large,
  body kept in the queue) stays pending, and the normal TTL alarm applies.
- UA2b item 3: except a placeholder (or redacted body) of a message whose
  owner push went out in this process (``local_target.hold_unpushed_placeholder``): the
  owner already has the text from the push, so the row is acknowledged
  and its sender is never told it expired unread.  The placeholder still
  releases no body (S2): acknowledging consumes the queued row, it grants
  nothing.  An unpushed placeholder (a reply, or a push with nothing
  configured) keeps its body queued for the operator until the TTL; one
  whose push goes out later is acknowledged then (``note_user_pushed``).
- Only while this node holds the owner's user agent; otherwise nothing here
  touches a row.
- An ack that is refused or raises is raised to the recorder, which keeps
  the row and hands it back on later passes while it is still awaited
  (UA2b item 5), so one failed ack is retried instead of being left to the
  TTL.  A daemon still restoring refuses ``message.ack`` the same way.
- UA2b item 6: ``awaiting`` answers None while this node has an owner but
  not yet the owner's user agent, so the recorder's one-time replay waits
  for it instead of being marked done with nothing awaited.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.delivery.ports import SOURCE_INBOX, InteractionRow
from hyprial.daemon.impl.user_agent.local_target import (
    bind_local_user_target,
    hold_unpushed_placeholder,
)
from hyprial.kernel import canonical_user_uri

__all__ = ["UserRowConsumer"]


class UserRowConsumer:
    """Recorder observer that acknowledges recorded rows to ``user:<owner>``."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _principal(self) -> str | None:
        app = self._app
        owner = getattr(app, "owner", None)
        if not owner or not bind_local_user_target(app):
            return None
        return canonical_user_uri(owner)

    def awaiting(self) -> frozenset[str] | None:
        if not getattr(self._app, "owner", None):
            return frozenset()
        principal = self._principal()
        if principal is None:
            return None
        return frozenset(
            message.message_id for message in self._app._inbox.pending_messages(principal)
        )

    def recorded(self, row: InteractionRow, *, intact: bool) -> None:
        principal = self._principal()
        message = row.message
        if principal is None or row.source != SOURCE_INBOX or message.recipient != principal:
            return
        if not intact and not hold_unpushed_placeholder(self._app, message.message_id):
            return
        result = self._app.handle(
            "message.ack",
            {"from": principal, "messageId": message.message_id},
            _trusted_message_origin="daemon",
        )
        acknowledged = isinstance(result, dict) and bool(result.get("acknowledged"))
        self._app._log(
            "info" if acknowledged else "warn",
            "conversations",
            "conversation.user_row_acknowledged",
            messageId=message.message_id,
            acknowledged=acknowledged,
        )
        if not acknowledged:
            raise RuntimeError(f"message.ack did not acknowledge {message.message_id}")
