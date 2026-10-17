"""The send boundary's link gate: ``message.send`` / ``message.reply`` (§2.4).

``send.py`` still owns target resolution, payload shape and the existing
delivery paths.  This module owns the four things WP2 adds around them:

* ask the one bound ``LinkAdmission.gate`` before a governed agent send and
  turn its ``Allow`` / ``Mint`` / ``Hold`` into durable outbox state
  (``link_open`` / ``link_hold``) plus the ``holdReason`` the caller sees;
* recognise a replay by its deterministic message id and return the durable
  submission receipt instead of asking the gate a second time (B6);
* ``user:`` targets: the gate is still asked, so ruling 53's synchronous
  ``LINK_WINDOW_FULL`` rejection works, a replay returns the original
  result without a second gate ask (B6), and the outbound ledger row is
  written before delivery, released on any failure, and booked only for
  this node's own agents;
* ``message.reply``: gate the reply before anything is acknowledged, keep a
  held reply from acking its original, and write ``replyTo`` into the body
  for agent targets (ruling 55).

Only the shared ``links.ports`` seam is imported; the engine itself is never
imported here, and this module never writes into ``links/``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors, parse_agent_uri
from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS
from hyprial.daemon.impl.inbox.contracts.api import LinkLedgerOpen
from hyprial.daemon.impl.inbox.contracts.ports import UpdateLinkLedgerCommand
from hyprial.daemon.impl.links.ports import (
    HOLD_REASON_KEY,
    Allow,
    Hold,
    LinkGate,
    LinkSendObserver,
    Mint,
)

#: Fallback ledger expiry when the inbox authority does not expose its TTL.
_DEFAULT_LEDGER_TTL_MS = DEFAULT_HOLD_TTL_MS


@dataclass(frozen=True, slots=True)
class LinkSendPlan:
    """What the send boundary must do with one outgoing message (WP2 §2.4).

    ``hold_reason`` is None when the message may be delivered now.  A held
    message carries ``link_hold`` on its outbox row and reports
    ``holdReason`` (never ``reason``) to the caller.  ``ledger_open`` is the
    outbound ledger row to commit in the submit transaction; it is None for
    exemptions and internal actors (no bookkeeping, §2.2 step 2).
    """

    hold_reason: str | None = None
    detail: str | None = None
    evicted: str | None = None
    peer: str | None = None
    ledger_open: LinkLedgerOpen | None = None
    replay: bool = False

    @property
    def held(self) -> bool:
        return self.hold_reason is not None

    def delivery_fields(self) -> dict[str, Any]:
        """The delivery-result keys a hold adds.  Empty when allowed."""

        if self.hold_reason is None:
            return {}
        fields: dict[str, Any] = {HOLD_REASON_KEY: self.hold_reason}
        if self.detail:
            fields["detail"] = self.detail
        if self.evicted is not None:
            fields["evicted"] = self.evicted
        return fields

    def submit_kwargs(self) -> dict[str, Any]:
        """The extra ``submit`` keywords this plan needs.

        Empty for an exempt target, so a caller that predates the link
        columns keeps working unchanged (the real inbox has accepted
        ``link_hold``/``link_open`` since WP2-B1).
        """

        if self.hold_reason is None and self.ledger_open is None:
            return {}
        return {
            "link_hold": self.hold_reason,
            "link_open": self.ledger_open,
        }


class _LinkSendMixin:
    """Send-boundary link gate glue for the ``DaemonApplication`` cluster."""

    # -- reads -----------------------------------------------------------

    def _link_slot(self) -> Any:
        """The one admission slot (README ruling 67), or None in bare tests."""

        return getattr(self, "link_admission", None)

    def _link_gate(self) -> LinkGate | None:
        slot = self._link_slot()
        return None if slot is None else slot.gate

    def _link_governs(self, sender: str, target: str) -> bool:
        slot = self._link_slot()
        if slot is None:
            return False
        return bool(slot.governs(sender, target))

    @staticmethod
    def _link_now_ms() -> int:
        return time.time_ns() // 1_000_000

    def _link_ttl_ms(self) -> int:
        value = getattr(getattr(self, "_inbox", None), "durable_ttl_ms", None)
        if isinstance(value, int) and value > 0:
            return value
        return _DEFAULT_LEDGER_TTL_MS

    @staticmethod
    def _link_reply_to_param(value: object) -> str | None:
        """Ruling 39: ``replyTo`` is a non-empty string or absent, never ``""``."""

        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "replyTo must be a non-empty string",
            )
        return value

    # -- message.send ----------------------------------------------------

    def _link_ledger_open(
        self,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        message_id: str,
        now_ms: int,
        state: str,
    ) -> LinkLedgerOpen:
        return LinkLedgerOpen(
            actor=sender,
            message_id=message_id,
            peer=target,
            intent=intent,
            reply_to=reply_to,
            state=state,
            opened_at_ms=now_ms,
            expires_at_ms=now_ms + self._link_ttl_ms(),
        )

    def _link_send_plan(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        message_id: str,
        now_ms: int | None = None,
    ) -> LinkSendPlan:
        """The gate's answer for one governed agent target (§2.4 step 3–5).

        Exempt targets (self, ``route:``, internal actor names, non-agent
        senders) are not governed: no gate call, no ledger row (§2.2 step 2).
        A governed target with no bound gate holds fail-closed (ruling 54).
        """

        now = self._link_now_ms() if now_ms is None else now_ms
        if not self._link_governs(sender, target):
            return LinkSendPlan()
        gate = self._link_gate()
        if gate is None:
            return LinkSendPlan(
                hold_reason=ipc_errors.LINK_SELF_FULL,
                detail="gate-unavailable",
                ledger_open=self._link_ledger_open(
                    sender, target, intent, reply_to, message_id, now, "held"
                ),
            )
        return self._link_decision(
            gate,
            sender=sender,
            target=target,
            intent=intent,
            reply_to=reply_to,
            message_id=message_id,
            now_ms=now,
        )

    def _link_decision(
        self,
        gate: LinkGate,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        message_id: str,
        now_ms: int,
    ) -> LinkSendPlan:
        decision = gate.admit_send(
            sender=sender,
            target=target,
            intent=intent,
            reply_to=reply_to,
            now_ms=now_ms,
        )
        if isinstance(decision, Allow):
            return LinkSendPlan(
                peer=decision.peer,
                ledger_open=self._link_ledger_open(
                    sender, target, intent, reply_to, message_id, now_ms, "open"
                ),
            )
        if isinstance(decision, Mint):
            return LinkSendPlan(
                hold_reason=ipc_errors.LINK_MINTING,
                detail="minting",
                evicted=decision.evict,
                peer=decision.peer,
                ledger_open=self._link_ledger_open(
                    sender, target, intent, reply_to, message_id, now_ms, "held"
                ),
            )
        if isinstance(decision, Hold):
            return LinkSendPlan(
                hold_reason=decision.reason,
                detail=decision.detail,
                peer=decision.peer,
                ledger_open=self._link_ledger_open(
                    sender, target, intent, reply_to, message_id, now_ms, "held"
                ),
            )
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"unknown link decision {decision!r}",
        )

    def _link_replay_delivery(
        self, message_id: str, *, target: str
    ) -> dict[str, Any] | None:
        """The durable receipt of an earlier identical send, or None (B6).

        The message id is derived from the operation id, so a replayed send
        reaches the same id.  A receipt short-circuits the whole boundary: no
        gate call, no reservation, no mint, no delivery.
        """

        reader = getattr(self._inbox, "submission_result_for_message", None)
        if not callable(reader):
            return None
        try:
            receipt = reader(message_id)
        except Exception:  # noqa: BLE001 - a replay read never blocks a send
            return None
        if receipt is None:
            return None
        fields: dict[str, Any] = {
            "target": target,
            "messageId": message_id,
            "accepted": bool(receipt.accepted),
            "queued": bool(receipt.queued),
        }
        if receipt.code:
            fields["code"] = receipt.code
        hold = self._link_outbox_hold(message_id)
        if hold is not None:
            fields["queued"] = True
            fields[HOLD_REASON_KEY] = hold
        return fields

    def _link_outbox_hold(self, message_id: str) -> str | None:
        inbox = self._inbox
        reader = getattr(inbox, "outbox_item_for_message", None)
        item: Any = None
        if callable(reader):
            item = reader(message_id)
        else:
            strict = getattr(inbox, "outbox_item", None)
            if callable(strict):
                try:
                    item = strict(message_id)
                except KeyError:
                    item = None
        return None if item is None else getattr(item, "link_hold", None)

    def _link_note_submitted(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        reply_to: str | None,
        hold_reason: str | None,
        now_ms: int | None = None,
    ) -> None:
        """Tell the link service what durable state the message reached (§2.2).

        Only ever called after the submit was durably accepted and never for a
        rejected or unknown outcome (B6).
        """

        gate = self._link_gate()
        if gate is None or not isinstance(gate, LinkSendObserver):
            return
        gate.submitted(
            sender=sender,
            target=target,
            message_id=message_id,
            reply_to=reply_to,
            hold_reason=hold_reason,
            now_ms=self._link_now_ms() if now_ms is None else now_ms,
        )

    # -- user: targets ---------------------------------------------------

    def _link_user_plan(
        self,
        *,
        sender: str,
        target: str,
        intent: str,
        reply_to: str | None,
        message_id: str,
        now_ms: int | None = None,
    ) -> LinkSendPlan:
        """Ask the gate for a ``user:`` target and plan its ledger row (§3.8).

        ``user:`` targets are not governed, but ruling 53 still measures the
        sender's W: the gate raises ``LINK_WINDOW_FULL`` synchronously for a
        non-owner request when W is full.  A replay is recognised before the
        gate is asked (B6) and plans no reservation or ledger row; the
        caller still delivers it, so ``user_delivery`` answers.  A new request from
        one of this node's own agents gets a durable ``out`` row; a reply
        closes the original instead and writes none.
        """

        now = self._link_now_ms() if now_ms is None else now_ms
        if self._link_user_is_replay(sender, message_id, reply_to):
            return LinkSendPlan(replay=True)
        gate = self._link_gate()
        if gate is not None:
            gate.admit_send(
                sender=sender,
                target=target,
                intent=intent,
                reply_to=reply_to,
                now_ms=now,
            )
        if reply_to is not None:
            return LinkSendPlan()
        if not self._link_sender_books_ledger(sender):
            return LinkSendPlan()
        return LinkSendPlan(
            ledger_open=self._link_ledger_open(
                sender, target, intent, reply_to, message_id, now, "open"
            )
        )

    def _link_user_is_replay(
        self, sender: str, message_id: str, reply_to: str | None = None
    ) -> bool:
        """Is this a retry of a ``user:`` send this node already admitted (B6)?

        This only answers "skip the gate and the second ledger row".  It is
        NOT proof of delivery: the ``out`` row is written before delivery, so
        a replay is always still sent through ``user_delivery``, whose
        durable duplicate result carries the real outcome.  A reply writes no
        ``out`` row; its first send closes the original inbound row, which is
        the durable sign that the gate already admitted it.
        """

        try:
            if reply_to is None:
                reader = getattr(self._inbox, "link_outbound_row", None)
                return callable(reader) and reader(sender, message_id) is not None
            snapshot = getattr(self._inbox, "link_ledger_snapshot", None)
            if not callable(snapshot):
                return False
            return any(
                row.direction == "in"
                and row.actor == sender
                and row.message_id == reply_to
                and row.state == "closed"
                for row in snapshot(self._link_now_ms())
            )
        except Exception:  # noqa: BLE001 - a replay read never blocks a send
            return False

    def _link_sender_books_ledger(self, sender: str) -> bool:
        """Only this node's own agents book durable ``out`` rows (§3.8).

        Adapter and ``user:`` senders ride the delivery path but own no
        link state; their rows would sit unreconciled in the ledger until
        the TTL purge (wp2b-acceptance-1 §六).
        """

        parsed = parse_agent_uri(sender)
        if parsed is None:
            return False
        owner, node, _actor = parsed
        return owner == getattr(self, "owner", None) and node == getattr(
            self, "node_id", None
        )

    def _link_user_open(self, row: LinkLedgerOpen) -> None:
        self._link_update_ledger(
            f"link:open:{row.actor}:{row.message_id}",
            open_outbound=(row,),
        )

    def _link_user_release(self, sender: str, message_id: str) -> None:
        self._link_update_ledger(
            f"link:release:{sender}:{message_id}",
            release_outbound=((sender, message_id),),
        )

    def _link_user_sent(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        reply_to: str | None,
        now_ms: int | None = None,
    ) -> None:
        now = self._link_now_ms() if now_ms is None else now_ms
        if reply_to is not None:
            self._link_update_ledger(
                f"link:close:{sender}:{reply_to}",
                close_inbound=((sender, reply_to, now),),
            )
        self._link_note_submitted(
            sender=sender,
            target=target,
            message_id=message_id,
            reply_to=reply_to,
            hold_reason=None,
            now_ms=now,
        )

    def _link_update_ledger(self, correlation_id: str, **fields: Any) -> None:
        updater = getattr(self._inbox, "update_link_ledger", None)
        if not callable(updater):
            return
        updater(UpdateLinkLedgerCommand(correlation_id=correlation_id, **fields))

    # -- message.reply ---------------------------------------------------

    def _link_reply_plan(
        self,
        *,
        sender: str,
        target: str,
        message_id: str,
        now_ms: int | None = None,
    ) -> LinkSendPlan:
        """The gate's answer for one governed reply (§2.4 message.reply).

        The reply is gated before the original is acknowledged: a ``Hold``
        keeps the question pending, an exception (``LINK_RECLAIMED``,
        ``MESSAGE_REPLY_UNAVAILABLE``, ``INVALID_ARGUMENT``) propagates.
        """

        now = self._link_now_ms() if now_ms is None else now_ms
        if not self._link_governs(sender, target):
            return LinkSendPlan()
        gate = self._link_gate()
        if gate is None:
            return LinkSendPlan(
                hold_reason=ipc_errors.LINK_SELF_FULL,
                detail="gate-unavailable",
            )
        decision = gate.admit_send(
            sender=sender,
            target=target,
            intent="reply",
            reply_to=message_id,
            now_ms=now,
        )
        if isinstance(decision, Allow):
            return LinkSendPlan(peer=decision.peer)
        if isinstance(decision, Mint):
            return LinkSendPlan(
                hold_reason=ipc_errors.LINK_MINTING,
                detail="minting",
                evicted=decision.evict,
                peer=decision.peer,
            )
        return LinkSendPlan(
            hold_reason=decision.reason,
            detail=decision.detail,
            peer=decision.peer,
        )

    def _link_reply_unavailable(
        self, *, sender: str, target: str, message_id: str
    ) -> None:
        """Let the gate name the code for a reply whose original is gone (B5).

        The drained set answers ``LINK_RECLAIMED``; everything else answers
        ``MESSAGE_REPLY_UNAVAILABLE`` here.  A returning gate (for example an
        unbound one) falls through to that same code.
        """

        gate = self._link_gate()
        if gate is None:
            return
        gate.admit_send(
            sender=sender,
            target=target,
            intent="reply",
            reply_to=message_id,
            now_ms=self._link_now_ms(),
        )
