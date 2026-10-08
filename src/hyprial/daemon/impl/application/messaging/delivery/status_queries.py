"""Delivery status queries: the message.status projection and its row codecs."""

from __future__ import annotations

from __future__ import annotations
import json
from typing import TYPE_CHECKING
from hyprial.daemon.impl.inbox import (
    DeliveryStatus,
    InboxMessage,
    OutboxItem,
    StatusQueryReport,
    StatusQueryServed,
    TerminalState,
    conflicting_message_ids,
    merge_delivery_status,
    query_delivery_status,
)
from hyprial.kernel import (
    canonical_agent_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import (
    _canonical_holder_name,
)
from hyprial.daemon.impl.application.messaging.delivery.sends import (
    _undeliverable_outbox_recipient,
)
from hyprial.daemon.impl.application.messaging.visibility import (
    _visible_message_origin,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _actor,
    _required_string,
)


def _query_entry(message: InboxMessage, *, kind: str) -> JsonObject:
    """One row of ``message.query``: the fields a person reads, text included."""

    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    text = body.get("message", "") if isinstance(body, dict) else ""
    entry: JsonObject = {
        "kind": kind,
        "messageId": message.message_id,
        "conversationId": message.conversation_id,
        "from": message.sender,
        "to": message.recipient,
        "intent": message.intent,
        "createdAtMs": message.created_at_ms,
        "message": text if isinstance(text, str) else str(text),
    }
    entry["origin"] = _visible_message_origin(body, message.sender)
    return entry

def _outbox_entry_json(item: OutboxItem) -> JsonObject:
    return {
        "messageId": item.message.message_id,
        "conversationId": item.message.conversation_id,
        "sender": item.message.sender,
        "recipient": item.message.recipient,
        "intent": item.message.intent,
        "createdAtMs": item.message.created_at_ms,
        "attempts": item.attempts,
        "nextAttemptMs": item.next_attempt_ms,
        "expiresAtMs": item.expires_at_ms,
        "undeliverableScheme": _undeliverable_outbox_recipient(
            item.message.recipient
        ),
    }

def _status_query_timeout(params: JsonObject) -> float:
    """Bound the mesh leg of message.status; the local read never blocks."""

    raw = params.get("timeoutSeconds")
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), 10.0)
    return 2.0


class _DeliveryStatusMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _log_status_query(self, served: StatusQueryServed) -> None:
        """Record every ``msg/status`` query this node answered, and how."""

        self._log(
            "info",
            "daemon",
            "message.status.served",
            selector=served.selector,
            querySender=served.sender,
            messageId=served.message_id,
            records=served.records,
            answered=served.answered,
        )

    def _delivery_status_result(
        self,
        params: JsonObject,
        *,
        trusted_origin: str | None = None,
    ) -> JsonObject:
        """Read the persisted verdict on messages this sender sent.

        Local records first, then the mesh: custody is ownership transfer, so
        a message this daemon queued may now belong to a mailbox that will be
        the one to record its outcome.  The mesh leg is best effort -- a holder
        that is unreachable right now simply is not represented, which is why
        the record is persistent and re-pullable rather than pushed once.
        """

        assert self._inbox is not None
        caller = self._authenticated_message_caller(
            params,
            method="message.status",
            trusted_origin=trusted_origin,
        )
        sender = caller.subject
        if "sessionRef" in params or "onBehalfOf" in params:
            sender_keys = (sender,)
        else:
            # Records are keyed by the wire sender.  The send boundary
            # resolves bare names to registered identities (Allen A(b));
            # records from earlier eras carry the verbatim bare spelling or
            # the short-lived mint era's local four-segment spelling.  The
            # local read unions every spelling the name could have — losing
            # sight of durable rows we promised to keep is not an
            # acceptable form of breaking compatibility.  The mesh leg
            # stays resolved-only.  Start from the caller's own spelling, not
            # the authenticated subject, which is already canonical and would
            # drop the bare-name rows.
            sender = _actor(params)
            resolved = self._resolve_consumer_identity(sender)
            keys = [resolved]
            if resolved != sender:
                keys.append(sender)
            elif ":" not in sender:
                keys.append(canonical_agent_uri(self.owner, self.node_id, sender))
            sender_keys = tuple(keys)
            sender = resolved
        raw_message_id = params.get("messageId")
        message_id = (
            None
            if raw_message_id is None
            else _required_string(raw_message_id, "messageId")
        )
        local_by_id: dict[str, DeliveryStatus] = {}
        for key in sender_keys:
            for record in self._inbox.delivery_status_records(
                key, message_id=message_id
            ):
                local_by_id.setdefault(record.message_id, record)
        # A recipient asking about a message it RECEIVED: the sender-keyed
        # lookup above can never match (records are keyed by the original
        # sender), and the mesh query would fan out to every holder and come
        # back empty.  Its own node recorded the receipt, so that local row
        # is the answer and the mesh is not asked.  This is what a carrier's
        # ack-unavailable settlement needs; without it the carrier polled the
        # mesh every 5 s without end (codex-router, 2026-09-25: 56k queries).
        recipient_read = getattr(self._inbox, "delivery_status_for_recipient", None)
        if not local_by_id and message_id is not None and callable(recipient_read):
            for key in sender_keys:
                received = recipient_read(key, message_id)
                if received is not None:
                    local_by_id[received.message_id] = received
                    break
        local = tuple(local_by_id.values())
        received_locally = bool(local) and all(
            record.recipient in sender_keys for record in local
        )
        mesh = StatusQueryReport()
        mesh_error: str | None = None
        if self._transport is not None and not received_locally:
            try:
                mesh = query_delivery_status(
                    self._transport,
                    sender,
                    message_id=message_id,
                    timeout=_status_query_timeout(params),
                )
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - best-effort mesh read
                mesh_error = str(error)
                self._log(
                    "warn",
                    "daemon",
                    "message.status.mesh_unreachable",
                    sender=sender,
                    detail=mesh_error,
                )
        records: list[DeliveryStatus] = [*local, *mesh.records]
        conflicts = conflicting_message_ids(records)
        if conflicts:
            # The only case where the merge has to pick a winner, and so the
            # only case where a short set can be confidently wrong.
            self._log(
                "warn",
                "daemon",
                "message.status.conflicting_claims",
                sender=sender,
                messageIds=list(conflicts),
                holders=sorted({record.holder for record in records}),
            )
        merged = merge_delivery_status(records)
        projected_records: list[JsonObject] = []
        for record in merged:
            projected = record.to_json()
            projected_records.append(projected)
        local_holder = _canonical_holder_name(self.node_id)
        responded = {
            _canonical_holder_name(holder) for holder in mesh.responded_holders
        }
        missing_holders = sorted(
            {
                parsed[1]
                for record in merged
                if record.state is TerminalState.EXPIRED
                and (parsed := parse_agent_uri(record.recipient)) is not None
                and _canonical_holder_name(parsed[1]) != local_holder
                and _canonical_holder_name(parsed[1]) not in responded
            }
        )
        for dropped in mesh.undecodable:
            # A malformed record set is still a positive reply from its named
            # holder when the additive envelope metadata survived decoding.
            self._log(
                "warn",
                "daemon",
                "message.status.undecodable_reply",
                sender=sender,
                messageId=message_id,
                key=dropped.key,
                holder=dropped.holder,
                detail=dropped.detail,
            )
        for failed in mesh.errors:
            fields: JsonObject = {
                "sender": sender,
                "messageId": message_id,
                "key": mesh.key,
                "missingRecipientHolders": missing_holders,
                "detail": failed,
            }
            if len(missing_holders) == 1:
                fields["holder"] = missing_holders[0]
            self._log(
                "warn",
                "daemon",
                "message.status.reply_error",
                **fields,
            )
        if duplicates := mesh.duplicate_holders:
            # One name answered several times: the wire-side signature of two
            # daemons sharing one node identity.  Name-keyed diagnostics
            # (meshHolders, responded_holders) collapse these answers, so the
            # pull counts them while it still has the replies apart.  The
            # liveliness watch raises daemon.identity.duplicate_instance from
            # its own vantage; this is the same condition seen through a
            # status query, which only nodes holding records for this sender
            # can observe.
            duplicate_fields: JsonObject = {
                "sender": sender,
                "key": mesh.key,
                "holders": [
                    {
                        "holder": entry.holder,
                        "replies": entry.replies,
                        "records": entry.records,
                    }
                    for entry in duplicates
                ],
            }
            if message_id is not None:
                duplicate_fields["messageId"] = message_id
            self._log(
                "warn",
                "daemon",
                "message.status.duplicate_holder",
                **duplicate_fields,
            )
        result: JsonObject = {
            "ok": True,
            "from": sender,
            "records": projected_records,
            # Deliberately not a "complete" flag: how many holders exist is not
            # knowable, so no pull can claim completeness.  These are the raw
            # facts about how the set was assembled, which is what lets a
            # caller decide whether to trust one pull or pull again.
            "diagnostics": {
                "localRecords": len(local),
                "meshReplies": mesh.replies,
                "meshRecords": len(mesh.records),
                # meshHolders keeps its published meaning: holders whose
                # records are in the merged set (contract/e2e
                # 01-two-machine-roundtrip).  Nodes that answered with no
                # record are a separate fact, reported as meshResponders.
                "meshHolders": list(mesh.holders),
                "meshResponders": list(mesh.responded_holders),
                "undecodableReplies": len(mesh.undecodable),
                "undecodableHolders": list(mesh.undecodable_holders),
                "replyErrors": len(mesh.errors),
                "meshError": mesh_error,
                "holders": sorted({record.holder for record in records}),
                "missingRecipientHolders": missing_holders,
                "conflictingMessageIds": list(conflicts),
                "duplicateHolderRecords": [
                    {
                        "holder": entry.holder,
                        "replies": entry.replies,
                        "records": entry.records,
                    }
                    for entry in mesh.duplicate_holders
                ],
                "noKnownLoss": mesh_error is None and mesh.no_known_loss,
            },
        }
        if message_id is None:
            return result
        result["messageId"] = message_id
        if projected_records:
            result["state"] = projected_records[0]["state"]
            return result
        # No terminal record yet.  These two are deliberately NOT terminal
        # states: "pending" means this node still holds it and will itself
        # write the verdict, "unknown" means no reachable holder has one.
        held_expiry = self._inbox.held_expiry_ms(message_id)
        result["state"] = "pending" if held_expiry is not None else "unknown"
        if held_expiry is not None:
            result["holdExpiresAtMs"] = held_expiry
        return result
