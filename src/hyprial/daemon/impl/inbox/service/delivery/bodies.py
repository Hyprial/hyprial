"""Body release on the inbox state machine (cell model phase A, slice S5).

Decision notes (phase A, S5):

- The grant ledger and the retention sweep live in the pure
  ``delivery.retention`` authority; this mixin only lends them the
  connection, the one row decoder (``stored_message``) and the one class
  accessor (``message_class_of``).
- The sweep runs at the start of every inbox prune, on the actor thread,
  first over granted INTERACTION bodies, then over SYSTEM bodies by the
  code default (``sweep_system_residue``, slice T).  It
  rewrites only consumed or settled rows and records no committed change:
  pending, outbox and custody projections are unchanged by a drop, so the
  version waiters are not woken for it.
- A grant is serialized like every other mutation and also records no
  committed change, for the same reason.
"""

from __future__ import annotations

from hyprial.daemon.impl.delivery import (
    BodyGrant,
    apply_body_grant,
    sweep_body_retention,
    sweep_system_residue,
)
from hyprial.daemon.impl.inbox.envelope.columns import stored_message
from hyprial.daemon.impl.inbox.envelope.rules import message_class_of
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized


class InboxServiceBodiesMixin:
    @_synchronized
    def grant_body_release(
        self, grant: BodyGrant, *, retention_ms: int, now_ms: int | None = None
    ) -> bool:
        """Record one body release grant; False when it names no live row."""

        return apply_body_grant(
            self._db,
            grant,
            retention_ms=retention_ms,
            now_ms=self._now_ms() if now_ms is None else now_ms,
            decode=stored_message,
            class_of=message_class_of,
        )

    def _sweep_body_retention(self, now_ms: int) -> int:
        return sum(
            sweep(
                self._db,
                now_ms=now_ms,
                decode=stored_message,
                class_of=message_class_of,
            )
            for sweep in (sweep_body_retention, sweep_system_residue)
        )
