"""Read public progress against durable PAC delivery identities, never aliases."""

from hyprial.inbox.progress import decode_progress_event
from hyprial.pac.store import PacGraphStore


def observe_node(database, graph, node, inbox, *, recipient: str, at: int, epoch: str):
    store = PacGraphStore(database, read_only=True)
    try:
        rows = store._db.execute(
            "SELECT message_id,request_id FROM workflow_deliveries WHERE graph_id=? AND node_id=? ORDER BY rowid DESC LIMIT 33",
            (graph["graphId"], node["nodeId"]),
        ).fetchall()
    finally:
        store.close()
    deliveries = [
        {
            "deliveryId": row["message_id"],
            "requestId": row["request_id"],
            "actor": node["owner"],
        }
        for row in rows[:32]
    ]
    conversation = f"pac-{graph['graphId']}"
    events = []
    for delivery in deliveries:
        for message in inbox.list_progress_events(
            recipient, delivery_id=delivery["deliveryId"]
        ):
            event = decode_progress_event(message.payload)
            if (
                event is not None
                and event.delivery_id == delivery["deliveryId"]
                and event.actor == node["owner"]
                and event.conversation_id == conversation
                and message.sender == node["owner"]
                and message.recipient == recipient
                and message.conversation_id == conversation
            ):
                events.append(event.to_payload_dict())
    events.sort(key=lambda e: (e["emittedAtMs"], e["deliveryId"], e["seq"]))
    seqs = {}
    gaps = len(rows) > 32
    for event in events:
        previous = seqs.get(event["deliveryId"], -1)
        gaps = gaps or event["seq"] > previous + 1 or bool(event.get("droppedSinceSeq"))
        seqs[event["deliveryId"]] = event["seq"]
    return {
        "schemaVersion": 2,
        "backend": "pac",
        "graphId": graph["graphId"],
        "runId": graph["graphId"],
        "target": node["nodeId"],
        "node": node,
        "sender": graph["sender"],
        "owner": node["owner"],
        "tracking": node,
        "conversationId": conversation,
        "observedAt": at,
        "daemonEpoch": epoch,
        "requestId": node["requestId"],
        "flag": node["flag"],
        "reasonRef": node["reasonRef"],
        "deliveries": deliveries,
        "identityAvailable": bool(deliveries),
        "progress": {
            "events": events[-200:],
            "gaps": gaps,
            "truncated": len(events) > 200,
        },
        "results": {
            "replies": [],
            "evidenceRef": node["reasonRef"],
            **(
                {"outputText": node["outputText"]}
                if "outputText" in node
                else {}
            ),
        },
    }
