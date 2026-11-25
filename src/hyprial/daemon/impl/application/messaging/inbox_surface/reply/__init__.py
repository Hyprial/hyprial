"""message.reply: answer one pending message and acknowledge the original.

Decision notes:

- Split out of ``inbox_surface/send.py`` (conversation plan slice P2) as a
  pure move, so the reply path and the send path can change independently and
  each file keeps room under the 1000-line limit.
- ``DaemonApplication`` lists ``_MessageReplyMixin`` directly: the inbox
  authority gate binds the raw ``submit``/``ack`` sites in
  ``_ipc_message_reply`` to the state host through that exact base.
- Slice GC3: a pending row whose conversation is a group id is answered in
  the group (``inbox_surface.groups.messages.reply_in_group``), before any
  recipient resolution or submit, and is then acknowledged
  here, at this method's acknowledgement site; it never becomes a 1:1
  reply.  Without a pending row, a group row this caller held is answered
  only when the group document already holds its reply (a retry).
- T1b (CM-PLAN「摘掉调用」) removed the link gate from this boundary: a
  reply submits without a hold, never reports ``holdReason``, and always
  acknowledges its original once the reply is durably admitted.
- Group reply default mention (2026-11-21): ``message.reply`` accepts optional
  ``mentions`` (member id array) and ``noMention`` (bool). For a group reply,
  the daemon passes both to ``reply_in_group``, which records the replied-to
  author in ``mentions`` by default (only when that author is another current
  agent member) plus any explicit ``mentions``; a 1:1 reply rejects non-None
  ``mentions`` or ``noMention=True`` with ``INVALID_ARGUMENT`` and otherwise
  leaves 1:1 behavior byte-identical.
- Recipient resolution: a bare sender is resolved with the same pipeline
  message.send applies to its targets (a bare spelling would be an outbox
  island no mailbox drains); user: and other scheme-carrying senders pass
  through untouched, and an ambiguous alias fails loudly.  A node-address
  recipient is refused with ``TARGET_IS_NODE`` and the original stays pending.
- F1c (2026-11-24): a recipient with no reply bridge and no deliverable
  address form (the retired ``channel:`` spelling, any unknown scheme) is
  refused with ``MESSAGE_REPLY_UNAVAILABLE`` before submit, so the original
  stays pending instead of being acked into an undeliverable outbox row.
"""

from __future__ import annotations
import json
import time
from typing import Any
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.kernel import (
    DaemonRequestError,
    ipc_errors,
    is_group_id_segment,
    reply_message_id,
)
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.summary import check_summary
from hyprial.daemon.impl.inbox.envelope import inherit_envelope
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    TARGET_KIND_AGENT,
    TARGET_KIND_HOST,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_boolean,
    _required_string,
)
from hyprial.daemon.impl.user_agent.local_target import canonical_user_end
from hyprial.daemon.impl.application.messaging.delivery.sends import (
    _undeliverable_outbox_recipient,
)


class _MessageReplyMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_message_reply(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        # Validated before any side effect: a refused reply does not ack the
        # original request.
        summary = check_summary(params.get("summary"), required=True)
        actor = caller.subject
        self._queue_agent_activity(actor)
        raw_resources = params.get("resourcePaths")
        if raw_resources is not None and (
            not isinstance(raw_resources, list) or raw_resources
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_RESOURCES_UNSUPPORTED,
                "harness_reply attachments are not implemented; the "
                "original pending message was not acknowledged and no "
                "reply was queued",
            )
        message_id = _required_string(params.get("messageId"), "messageId")
        text = _required_string(params.get("message"), "message")
        no_mention = _optional_boolean(params.get("noMention"), "noMention")
        raw_mentions = params.get("mentions")
        if raw_mentions is not None and (
            not isinstance(raw_mentions, list)
            or not all(isinstance(item, str) and item for item in raw_mentions)
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "mentions must be an array of member ids",
            )
        original_key = actor
        original = None
        original_is_pending = True
        consumer_keys = self._message_consumer_keys(params, actor=actor)
        for key in consumer_keys:
            original = next(
                (
                    item
                    for item in self._inbox.pending_messages(key)
                    if item.message_id == message_id
                ),
                None,
            )
            if original is not None:
                original_key = key
                break
        if original is None:
            # Background reply settlement acknowledges the inbound row,
            # removing it from pending_messages before a caller's next
            # retry.  Join only an existing deterministic reply receipt;
            # the retained inbound row supplies the original route and
            # fences the receipt to this actor's consumer key.  A later
            # submit below checks the receipt's command digest, including
            # the reply text, without repeating native I/O.
            reply_id = reply_message_id(message_id)
            receipt_reader = getattr(
                self._inbox, "reply_submission_result", None
            )
            original_reader = getattr(
                self._inbox, "harness_failure_original", None
            )
            existing = (
                receipt_reader(reply_id) if callable(receipt_reader) else None
            )
            retained = (
                original_reader(message_id) if callable(original_reader) else None
            )
            group_retry = (
                retained is not None
                and retained.recipient in consumer_keys
                and is_group_id_segment(retained.conversation_id)
            )
            if not group_retry and (
                existing is None
                or retained is None
                or retained.recipient not in consumer_keys
            ):
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                    "pending message was not found",
                )
            original = retained
            original_key = retained.recipient
            original_is_pending = False
        if is_group_id_segment(original.conversation_id):
            from ..groups.messages import group_reply_answer, reply_in_group

            entry = reply_in_group(
                self,
                sender=caller.sender,
                row=original,
                text=text,
                summary=summary,
                pending=original_is_pending,
                mentions=raw_mentions,
                no_mention=bool(no_mention),
            )
            return group_reply_answer(
                message_id,
                entry,
                self._inbox.ack(original_key, message_id) if original_is_pending else None,
            )
        if raw_mentions is not None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "mentions are only for a group conversation",
            )
        if no_mention:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "noMention is only for a group conversation",
            )
        adapter_name = lark_reply_adapter(original.sender)
        if (
            original_is_pending
            and original.sender.startswith(ADAPTER_URI_PREFIX)
            and adapter_name is None
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                "the pending message came from a channel actor without a "
                "reply bridge",
            )
        if (
            original_is_pending
            and adapter_name is not None
            and self._adapters is not None
            and adapter_name not in self._lark_gateway_names()
        ):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                f"Lark reply adapter is not configured: {adapter_name}",
            )
        reply_origin: JsonObject = {"via": caller.via}
        if caller.on_behalf_of is not None:
            reply_origin["onBehalfOf"] = caller.on_behalf_of
        reply_body: JsonObject = {"message": text, "origin": reply_origin}
        # The explicit association is stored for every reply, not only the
        # adapter bridge: the gate and the link RTT read it back (R2).
        reply_body["replyTo"] = message_id
        recipient = canonical_user_end(
            self._resolve_agent_alias(normalize_agent_recipient(original.sender)),
            self.agents,
        )
        if classify_target_identity(recipient) == TARGET_KIND_HOST:
            raise DaemonRequestError(
                ipc_errors.TARGET_IS_NODE,
                f"reply recipient {recipient!r} is a node address, not "
                "an agent: nodes are announced on the network but do "
                "not receive actor messages",
                {"target": recipient},
            )
        self._refuse_undeliverable_local_target(recipient)
        if adapter_name is None and _undeliverable_outbox_recipient(recipient):
            raise DaemonRequestError(
                ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                f"reply recipient {recipient!r} has no reply bridge and no "
                "deliverable address form; the original pending message was "
                "not acknowledged",
                {"target": recipient},
            )
        # Ruling 55: a reply to an agent carries its association too.  The
        # receiver's dispatch correlation and its outbound ledger close both
        # read it, and previously only adapter replies wrote it.
        if classify_target_identity(recipient) == TARGET_KIND_AGENT:
            reply_body["replyTo"] = message_id
        reply = InboxMessage(
            # Retrying the same harness_reply must address the same outbox
            # row.  Old rows use the same reply:<inbound-id> key but had a
            # random message id; the transport accepts both shapes.
            message_id=reply_message_id(message_id),
            conversation_id=original.conversation_id,
            sender=caller.sender,
            recipient=recipient,
            payload=json.dumps(reply_body, separators=(",", ":")).encode(),
            intent="reply",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            idempotency_key=f"reply:{message_id}",
            created_at_ms=time.time_ns() // 1_000_000,
            summary=summary,
        )
        reply = inherit_envelope(reply, original)
        thread_id = reply.thread_id or self._conversation_stamp(
            caller.sender, recipient
        )
        if thread_id is not None:
            reply = reply.with_envelope(thread_id=thread_id)
        try:
            submitted = self._inbox.submit(reply)
        except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
            if isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
                "SUBMISSION_RECEIPT_CONFLICT"
            ):
                receipt_reader = getattr(
                    self._inbox, "reply_submission_result", None
                )
                existing = None
                if callable(receipt_reader):
                    try:
                        existing = receipt_reader(reply.message_id)
                    except Exception as read_error:  # noqa: BLE001
                        self._log(
                            "warn",
                            "daemon",
                            "harness.reply.receipt_read_failed",
                            messageId=message_id,
                            replyMessageId=reply.message_id,
                            failure=type(read_error).__name__,
                        )
                settled = existing is not None and not existing.queued
                replied = bool(
                    settled and existing is not None and existing.accepted
                )
                acknowledged = False
                if replied:
                    acknowledged = (
                        True
                        if not original_is_pending
                        else self._inbox.ack(
                            original_key, message_id
                        ).acknowledged
                    )
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replyMessageId": reply.message_id,
                    "replied": replied,
                    "acknowledged": acknowledged,
                    "queued": existing.queued if existing is not None else True,
                    "code": "REPLY_ALREADY_SUBMITTED",
                    "retryable": False,
                    **({"settled": settled} if existing is not None else {}),
                }
            if isinstance(error, InboxAuthorityUnavailable) and (
                "CORRELATION_IN_FLIGHT" not in str(error)
            ):
                raise
            # Ambiguous external completion is not failure and must not
            # ACK the inbound question.  The deterministic reply message
            # id/correlation lets the caller retry and join the same
            # durable receipt without another native send.
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": True,
                "code": "REPLY_SETTLEMENT_PENDING",
                "retryable": True,
                "hint": "the reply is queued; retrying returns its state and never sends different text",
            }
        # A Lark bridge reply is not complete merely because its outbox
        # row was durably admitted.  ``queued`` means the native reply did
        # not succeed yet; the background outbox owns the single native
        # attempt and will settle the same stable receipt.  Keep the
        # inbound message pending and hand the caller the deterministic
        # reply id so a retry joins that receipt instead of triggering a
        # second native send or losing the user's question to an early ACK.
        if submitted.accepted and adapter_name is not None and submitted.queued:
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": True,
                "code": "REPLY_SETTLEMENT_PENDING",
                "retryable": True,
                "hint": "the reply is queued; retrying returns its state and never sends different text",
            }
        if not submitted.accepted:
            return {
                "ok": False,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": False,
                "acknowledged": False,
                "queued": submitted.queued,
                "code": submitted.code or "REPLY_DELIVERY_PENDING",
            }
        if not original_is_pending:
            return {
                "ok": True,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": True,
                "acknowledged": True,
                "queued": submitted.queued,
            }
        acknowledged = self._inbox.ack(original_key, message_id)
        return {
            "ok": acknowledged.acknowledged,
            "messageId": message_id,
            "replyMessageId": reply.message_id,
            "replied": True,
            "acknowledged": acknowledged.acknowledged,
            "queued": submitted.queued,
            **({"code": acknowledged.code} if acknowledged.code else {}),
        }
