"""``FakeInboxPort``: an in-memory ledger for the WP2 service tests.

Implements the ``LinkInboxPort`` protocol of ``links/store.py`` with held
outbox rows, live inbound rows and a per-actor ledger.  Failures, blocking and
partial success are injectable so the service's merge-only-real-success rules
(B15) and its non-blocking thread contract (B17) can be asserted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from hyprial.daemon.impl.links.store import (
    HeldRow,
    InboundRow,
    LedgerResult,
    LedgerRow,
    LedgerUpdate,
)

__all__ = ["FakeInboxPort"]


class FakeInboxPort:
    """An in-memory inbox ledger with injectable behaviour."""

    def __init__(
        self,
        *,
        fail_apply: bool = False,
        release_partial: Iterable[str] | None = None,
        ledger_block: Any | None = None,
        apply_block: Any | None = None,
        guard: Callable[[str], None] | None = None,
    ) -> None:
        self.rows: dict[tuple[str, str], LedgerRow] = {}
        self.held: dict[str, HeldRow] = {}
        self.inbound: dict[str, InboundRow] = {}
        self.bindings: dict[tuple[str, str, str], int] = {}
        self.applies: list[LedgerUpdate] = []
        self.fail_apply = fail_apply
        self.release_partial = (
            None if release_partial is None else set(release_partial)
        )
        self.ledger_block = ledger_block
        self.apply_block = apply_block
        self.guard = guard
        self.snapshot_calls = 0
        self.inbound_row_calls = 0
        self.service: Any | None = None

    # -- test helpers ---------------------------------------------------

    def attach_service(self, service: Any) -> None:
        self.service = service

    def put_out(
        self,
        actor: str,
        message_id: str,
        *,
        peer: str,
        intent: str = "request",
        reply_to: str | None = None,
        state: str = "open",
        opened_at_ms: int = 0,
        sent_at_ms: int | None = None,
        event_at_ms: int | None = None,
        sample_pending: bool = False,
        expires_at_ms: int = 1 << 62,
    ) -> LedgerRow:
        row = LedgerRow(
            actor=actor,
            message_id=message_id,
            direction="out",
            peer=peer,
            intent=intent,
            reply_to=reply_to,
            state=state,
            opened_at_ms=opened_at_ms,
            sent_at_ms=sent_at_ms,
            event_at_ms=event_at_ms,
            sample_pending=sample_pending,
            expires_at_ms=expires_at_ms,
        )
        self.rows[(actor, message_id)] = row
        return row

    def put_in(
        self,
        actor: str,
        message_id: str,
        *,
        peer: str,
        intent: str = "request",
        reply_to: str | None = None,
        state: str = "open",
        opened_at_ms: int = 0,
        event_at_ms: int | None = None,
        threshold_ms: int | None = None,
        sample_pending: bool = False,
        expires_at_ms: int = 1 << 62,
    ) -> LedgerRow:
        row = LedgerRow(
            actor=actor,
            message_id=message_id,
            direction="in",
            peer=peer,
            intent=intent,
            reply_to=reply_to,
            state=state,
            opened_at_ms=opened_at_ms,
            event_at_ms=event_at_ms,
            threshold_ms=threshold_ms,
            sample_pending=sample_pending,
            expires_at_ms=expires_at_ms,
        )
        self.rows[(actor, message_id)] = row
        return row

    def row_state(self, actor: str, message_id: str) -> str | None:
        row = self.rows.get((actor, message_id))
        return None if row is None else row.state

    def hold(
        self,
        message_id: str,
        *,
        sender: str,
        target: str,
        intent: str = "request",
        reply_to: str | None = None,
        reason: str,
        created_at_ms: int,
        expires_at_ms: int,
    ) -> HeldRow:
        row = HeldRow(
            message_id=message_id,
            sender=sender,
            target=target,
            intent=intent,
            reply_to=reply_to,
            hold_reason=reason,
            created_at_ms=created_at_ms,
            expires_at_ms=expires_at_ms,
        )
        self.held[message_id] = row
        if reply_to is None:
            self.put_out(
                sender,
                message_id,
                peer=target,
                intent=intent,
                state="held",
                opened_at_ms=created_at_ms,
                expires_at_ms=expires_at_ms,
            )
        return row

    def inject(
        self,
        actor: str,
        message_id: str,
        *,
        sender: str,
        intent: str = "request",
        reply_to: str | None = None,
        received_at_ms: int = 0,
        fetched_at_ms: int | None = None,
    ) -> InboundRow:
        row = InboundRow(
            message_id=message_id,
            sender=sender,
            recipient=actor,
            intent=intent,
            reply_to=reply_to,
            received_at_ms=received_at_ms,
            fetched_at_ms=fetched_at_ms,
            injected=True,
        )
        self.inbound[message_id] = row
        self.put_in(
            actor,
            message_id,
            peer=sender,
            intent=intent,
            reply_to=reply_to,
            state="open",
            opened_at_ms=received_at_ms,
        )
        service = self.service
        if service is not None:
            service.injected(
                actor=actor,
                message_id=message_id,
                sender=sender,
                intent=intent,
                reply_to=reply_to,
                received_at_ms=received_at_ms,
            )
        return row

    def queue_inbound(
        self,
        actor: str,
        message_id: str,
        *,
        sender: str,
        intent: str = "request",
        reply_to: str | None = None,
        received_at_ms: int = 0,
        fetched_at_ms: int | None = None,
    ) -> InboundRow:
        row = InboundRow(
            message_id=message_id,
            sender=sender,
            recipient=actor,
            intent=intent,
            reply_to=reply_to,
            received_at_ms=received_at_ms,
            fetched_at_ms=fetched_at_ms,
            injected=False,
        )
        self.inbound[message_id] = row
        return row

    def drop_held(self, message_id: str) -> None:
        self.held.pop(message_id, None)

    # -- LinkInboxPort --------------------------------------------------

    def ledger_snapshot(self, now_ms: int) -> tuple[LedgerRow, ...]:
        self._guard("ledger_snapshot")
        self.snapshot_calls += 1
        if self.ledger_block is not None:
            self.ledger_block.wait()
        return tuple(
            row for row in self.rows.values() if row.expires_at_ms > now_ms
        )

    def task_bindings(self) -> tuple[tuple[str, str, str, int], ...]:
        self._guard("task_bindings")
        return tuple(
            (actor, peer, task_id, bound_at_ms)
            for (actor, peer, task_id), bound_at_ms in sorted(self.bindings.items())
        )

    def held_outbox(self) -> tuple[HeldRow, ...]:
        self._guard("held_outbox")
        return tuple(self.held.values())

    def inbound_live(
        self, actor: str, peer: str | None = None
    ) -> tuple[InboundRow, ...]:
        self._guard("inbound_live")
        return tuple(
            row
            for row in self.inbound.values()
            if row.recipient == actor and (peer is None or row.sender == peer)
        )

    def inbound_row(self, message_id: str) -> InboundRow | None:
        self._guard("inbound_row")
        self.inbound_row_calls += 1
        return self.inbound.get(message_id)

    def apply(self, update: LedgerUpdate, *, now_ms: int) -> LedgerResult:
        self._guard("apply")
        self.applies.append(update)
        if self.apply_block is not None:
            self.apply_block.wait()
        if self.fail_apply:
            raise RuntimeError("fake apply failure")
        released: list[str] = []
        expired: list[str] = []
        failed: list[str] = []
        drained: list[tuple[str, str]] = []
        cancelled: list[str] = []
        for message_id in update.release:
            row = self.held.get(message_id)
            if row is None:
                continue
            if row.expires_at_ms <= now_ms:
                expired.append(message_id)
                continue
            if (
                self.release_partial is not None
                and message_id not in self.release_partial
            ):
                continue
            del self.held[message_id]
            released.append(message_id)
            self._transition_out(row.sender, message_id, "open")
            if row.reply_to is not None:
                # §3.6: releasing a held reply closes the original request and
                # acks it in the same transaction.
                self._transition_in(row.sender, row.reply_to, "closed", now_ms)
        for message_id, reason in update.reasons.items():
            row = self.held.get(message_id)
            if row is None:
                continue
            self.held[message_id] = HeldRow(
                message_id=row.message_id,
                sender=row.sender,
                target=row.target,
                intent=row.intent,
                reply_to=row.reply_to,
                hold_reason=reason,
                created_at_ms=row.created_at_ms,
                expires_at_ms=row.expires_at_ms,
            )
        for message_id, _code in update.fail.items():
            row = self.held.pop(message_id, None)
            if row is None:
                continue
            failed.append(message_id)
            self._transition_out(row.sender, message_id, "released")
        for actor, message_ids in update.drain.items():
            for message_id in message_ids:
                drained.append((actor, message_id))
                self._transition_in(actor, message_id, "drained", now_ms)
        for message_id, _code in update.cancel_outbound.items():
            cancelled.append(message_id)
            for (actor, existing), row in list(self.rows.items()):
                if existing == message_id and row.direction == "out":
                    self.rows[(actor, existing)] = _replace(row, state="released")
        for actor, message_id in update.release_outbound:
            self._transition_out(actor, message_id, "released")
        for record in update.censor:
            self._transition_in(
                record.actor,
                record.message_id,
                "censored",
                record.received_at_ms,
                threshold_ms=record.threshold_ms,
            )
        for actor, message_id in update.sampled:
            key = (actor, message_id)
            row = self.rows.get(key)
            if row is not None:
                self.rows[key] = _replace(row, sample_pending=False)
        for row in update.open_outbound:
            self.rows[(row.actor, row.message_id)] = row
        for actor, message_id, at_ms in update.close_inbound:
            self._transition_in(actor, message_id, "closed", at_ms)
        for actor, peer, task_id, bound_at_ms in update.bind_tasks:
            self.bindings[(actor, peer, task_id)] = bound_at_ms
        for actor, peer, task_id in update.unbind_tasks:
            self.bindings.pop((actor, peer, task_id), None)
        if update.purge_before_ms is not None:
            cutoff = update.purge_before_ms
            terminal = {"acked", "expired", "drained", "released", "closed"}
            for key, row in list(self.rows.items()):
                if row.state in terminal and row.expires_at_ms <= cutoff:
                    del self.rows[key]
        return LedgerResult(
            released=tuple(released),
            expired=tuple(expired),
            failed=tuple(failed),
            drained=tuple(drained),
            cancelled=tuple(cancelled),
        )

    # -- internals ------------------------------------------------------

    def _transition_out(self, actor: str, message_id: str, state: str) -> None:
        key = (actor, message_id)
        row = self.rows.get(key)
        if row is None:
            return
        self.rows[key] = _replace(row, state=state)

    def _transition_in(
        self,
        actor: str,
        message_id: str,
        state: str,
        at_ms: int,
        *,
        threshold_ms: int | None = None,
    ) -> None:
        key = (actor, message_id)
        row = self.rows.get(key)
        if row is None:
            return
        self.rows[key] = _replace(
            row,
            state=state,
            event_at_ms=at_ms,
            threshold_ms=threshold_ms if threshold_ms is not None else row.threshold_ms,
            sample_pending=state == "closed",
        )

    def _guard(self, op: str) -> None:
        if self.guard is not None:
            self.guard(op)


def _replace(row: LedgerRow, **changes: Any) -> LedgerRow:
    values = {
        "actor": row.actor,
        "message_id": row.message_id,
        "direction": row.direction,
        "peer": row.peer,
        "intent": row.intent,
        "reply_to": row.reply_to,
        "state": row.state,
        "opened_at_ms": row.opened_at_ms,
        "sent_at_ms": row.sent_at_ms,
        "event_at_ms": row.event_at_ms,
        "threshold_ms": row.threshold_ms,
        "sample_pending": row.sample_pending,
        "expires_at_ms": row.expires_at_ms,
    }
    values.update(changes)
    return LedgerRow(**values)
