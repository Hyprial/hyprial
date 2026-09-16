"""Bounded, read-only node evidence. Progress never settles a Workflow."""
from __future__ import annotations

import json
from hyprial.inbox.progress import decode_progress_event


def observe_node(context: dict, inbox: object, *, observed_at_ms: int, epoch: str) -> dict:
    deliveries = context["deliveries"]
    by_id = {d["deliveryId"]: d for d in deliveries}
    actors = {d["actor"] for d in deliveries}
    events = []
    # A legacy Run may have no durable resolved-recipient history. Do not
    # resolve its old alias using today's directory, or invent an attempt.
    for delivery in deliveries:
        for message in inbox.list_progress_events(context["sender"], delivery_id=delivery["deliveryId"]):
            event = decode_progress_event(message.payload)
            if (event is not None and event.delivery_id == delivery["deliveryId"]
                    and event.conversation_id == context["conversationId"]
                    and event.actor == delivery["actor"] and message.sender == delivery["actor"]
                    and message.conversation_id == context["conversationId"]
                    and message.recipient == context["sender"]):
                events.append(event.to_payload_dict())
    events.sort(key=lambda e: (e["emittedAtMs"], e["deliveryId"], e["seq"]))
    gaps = False
    seqs = {}
    for event in events:
        last = seqs.get(event["deliveryId"], -1)
        gaps = gaps or event["seq"] > last + 1 or bool(event.get("droppedSinceSeq"))
        seqs[event["deliveryId"]] = event["seq"]
    progress_truncated = len(events) > 200
    replies = []
    truncated = False
    remaining_bytes = 128 * 1024
    if actors:
        for message in inbox.workflow_replies(context["sender"], context["conversationId"]):
            if message.sender not in actors or message.intent != "reply":
                continue
            try:
                payload = json.loads(message.payload)
            except (ValueError, UnicodeError):
                continue
            text = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                continue
            reply_to = payload.get("replyTo")
            if reply_to is not None and not isinstance(reply_to, str):
                continue
            # Explicit links must match THIS dispatch. Older reply protocols
            # lack replyTo: exact run conversation + recorded actor identifies
            # the node, but cannot attribute a particular retry.
            if reply_to and (reply_to not in by_id or by_id[reply_to]["actor"] != message.sender):
                continue
            matches = context["awaitKind"] == "reply" and (not context["match"] or context["match"] in text)
            if remaining_bytes <= 0:
                truncated = True
                break
            excerpt = text.encode("utf-8")[:min(65536, remaining_bytes)].decode("utf-8", errors="ignore")
            if text and not excerpt:
                truncated = True
                break
            remaining_bytes -= len(excerpt.encode("utf-8"))
            replies.append({"messageId": message.message_id, "actor": message.sender,
                            "deliveryId": reply_to, "createdAtMs": message.created_at_ms,
                            "text": excerpt, "truncated": excerpt != text,
                            "matchesAwait": matches})
        truncated = truncated or len(replies) > 100
    replies = sorted(replies[:100], key=lambda r: (r["createdAtMs"], r["messageId"]))
    result = {"schemaVersion": 1, **context, "observedAt": observed_at_ms, "daemonEpoch": epoch,
            "identityAvailable": bool(deliveries),
            "progress": {"state": "available" if events else "empty" if deliveries else "unavailable",
                         "events": events[-200:], "gaps": gaps or progress_truncated,
                         "complete": False},
            "results": {"state": "available" if replies else "empty" if deliveries else "unavailable",
                        "replies": replies, "historyComplete": False, "truncated": truncated}}

    # IPC serializers may escape Unicode. Bound the actual JSON representation,
    # leaving envelope room beneath the GUI bridge's 1 MiB output limit.
    while len(json.dumps(result).encode()) > 768 * 1024:
        if result["progress"]["events"]:
            result["progress"]["events"].pop(0)
            result["progress"]["gaps"] = True
        elif result["results"]["replies"]:
            result["results"]["replies"].pop(0)
            result["results"]["truncated"] = True
        else:
            break
    return result
