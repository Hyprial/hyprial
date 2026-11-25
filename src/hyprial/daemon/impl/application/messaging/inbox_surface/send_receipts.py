"""Send-boundary receipts, stamps and the reply-association check (T1b).

Decision notes (CM-PLAN.md「T1b 摘掉调用」, owner rulings 2026-11-19 and
2026-11-20):

- This module is what remains of ``inbox_surface/link_send.py`` after the
  send-link gate was removed: nothing here asks a ``LinkGate``.  Kept
  pieces: the durable replay receipt read that makes an idempotent retry
  of one operation return its original outcome, the ``replyTo`` parameter
  validation the wire golden corpus pins (WG-036), the conversation
  attribution stamp every outbound row still carries (the ``t.``/``p.``
  key derivation is the pure ``conversations.direct_key``),
  and ``_thread_bind``, which keeps recording thread endpoints until the
  ``threads.*`` surface is removed (T2b).
- The reply-association checks (Δ2) survive without any link service: a
  ``replyTo`` that names no open request for the sender is
  ``MESSAGE_REPLY_UNAVAILABLE``; one that names another sender's request
  is ``INVALID_ARGUMENT``; a row that is itself a reply does not count.
  Text and data shapes are the ones ``links/service/gate.py`` used, so
  callers cannot tell the check moved.  Scope is unchanged too: internal
  actors and non-agent senders are not checked.
"""

from __future__ import annotations

from typing import Any

from hyprial.kernel import (
    RESERVED_AGENT_ACTOR_NAMES,
    DaemonRequestError,
    ipc_errors,
    parse_agent_uri,
    uri_scheme,
)
from hyprial.daemon.impl.ipc.params import JsonObject
from hyprial.daemon.impl.application.messaging.inbox_surface.queries import (
    _payload_reply_to,
)


def _is_governed_agent_pair(sender: str, target: str) -> bool:
    if sender == target:
        return False
    parsed_sender = parse_agent_uri(sender)
    parsed_target = parse_agent_uri(target)
    if parsed_sender is None or parsed_target is None:
        return False
    return (
        parsed_sender[2] not in RESERVED_AGENT_ACTOR_NAMES
        and parsed_target[2] not in RESERVED_AGENT_ACTOR_NAMES
    )


class _SendReceiptsMixin:
    """Send-boundary receipt and stamp glue for the ``DaemonApplication`` cluster."""

    def _conversation_stamp(self, sender: str, recipient: str) -> str | None:
        """The ``t.`` id of the conversation one message travels on, or None."""

        from hyprial.daemon.impl.conversations import direct_key

        try:
            key = direct_key(sender, recipient)
        except Exception:  # noqa: BLE001 - an unstamped row is still delivered
            return None
        return None if key is None else key.id

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

    def _replay_delivery(
        self, message_id: str, *, target: str
    ) -> dict[str, Any] | None:
        """The durable receipt of an earlier identical send, or None (B6).

        The message id is derived from the operation id, so a replayed send
        reaches the same id.  A receipt short-circuits the whole boundary: no
        second delivery attempt.
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
        return fields

    def _validate_reply_association(
        self,
        params: JsonObject,
        *,
        source: str,
        target: str,
        reply_to: str,
        user_queued: bool,
    ) -> None:
        """Δ2: the reply association must name an open request of this sender.

        Checked where the link gate used to be asked (a managed agent
        target, or this node's queued ``user:`` target) and only for an
        agent sender, exactly the pairs the gate governed.
        """

        if uri_scheme(source) != "agent":
            return
        if not user_queued and not _is_governed_agent_pair(source, target):
            return
        original: Any = None
        for key in self._message_consumer_keys(params, actor=source):
            for row in self._inbox.pending_messages(key):
                if row.message_id == reply_to:
                    original = row
                    break
            if original is not None:
                break
        if original is None or _payload_reply_to(original.payload) is not None:
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                f"no open request {reply_to!r} to reply to",
            )
        if uri_scheme(target) != "route" and original.sender != target:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"request {reply_to!r} came from {original.sender!r}, "
                f"not {target!r}",
            )
