"""Group rows: a group message reaches the member agents it wakes (slice GC4).

Decision notes (group chat design draft §4; owner decisions §10 and §10.1;
brief GC4):

- Only the agent members ``wake_members`` names get a row; a member it
  does not wake gets NO row and reads the group document from history.  By
  owner ruling (2026-11-21, superseding R28's first two clauses),
  ``wake_members`` wakes only the agent members named in ``mentions``, for
  human and agent authors alike, and ``replyTo`` implies no wake; an agent
  that needs to follow unmentioned messages adds a routine in runtime data
  to poll group history instead of hardcoding default wakes.  See
  docs/design/cell-conversation-refactor-inputs-2026-11-19.md section 4.3
  for the future cell-level judge harness (#2712).
- The row enters through the delivery transport's local receiver
  (``app._local_delivery.deliver`` -> the already-bound ``inbox.receive``,
  §10 8.2), the door a network frame uses: bare-sender gate, dedup,
  recipient-claimed, link ingress and capacity all apply.  This is a new
  producer into ``receive`` that the inbox authority gate's scanner does
  not see; it adds no manifest line.  No outbox row, no sender copy, no
  send-link plan: a group send mints no link (§10 8.4).  The owner's rows
  are link-exempt.  An agent-authored row between two agents of this owner
  passes ingress (its author has joined the network) and link dispatch
  admission keeps it out of the recipient's batch until a link between
  the two exists; such a held row waits for its TTL.  Delivery adds no
  other check: membership, checked by the send, is the gate.
- Single machine: a member is delivered to only when the local registry
  resolves its ``a.`` id to an agent of this owner on this node, by the
  registry's current address; nothing is ever forwarded to the network.
  The human member never gets a row.  A member whose ``joinedAtMs`` is
  after the entry, or who is no longer a member, gets nothing.
- Row (draft §4.3): id ``uuid5("hyprial:group-row:<g.id>:<entryId>:<memberId>")``,
  idempotency key ``group:<g.id>:<entryId>:<memberId>``, conversation id
  the group id, sender ``user:<owner>`` for the owner or the author's
  address, intent, summary and ``createdAtMs`` of the entry.  The payload
  is the text under ``message`` plus ``groupMessageId`` (the entry id, which
  ``message.reply`` answers) and, when present, ``mentions`` and
  ``replyTo`` (the entry's object form, which no 1:1 reader takes for a
  reply association: those read a string).
- Exactly once without a store of its own: the inbox dedups the row id and
  key for its dedup window (one day, ``ROW_WINDOW_MS``), durably, so a
  second pass, a retry or a restart answers duplicate.  An entry recorded
  more than that window ago is never delivered, so a pass cannot re-create
  a row its member already acknowledged.  A daemon stopped longer than the
  window between the entry and its rows delivers nothing for it.
- Two triggers: ``append_message`` (the one local write site of a message
  entry) delivers the entry it wrote and asks the dormant-agent wake, as a
  local send does; ``deliver_recent`` is the restart pass over every group
  document (composition runs it once at start).  Its rows are woken the
  ordinary way, by restore and the pending-work scan.  A delivery failure
  never fails the write: it is logged, and the next start delivers it.  A
  pass without the local delivery transport raises, so the restart pass
  retries instead of finishing with nothing delivered.
- Acks and expiry are a row's own (§4.7): ``message.reply`` or
  ``message.ack`` settle it, and it expires at the hold TTL without an
  expiry notice to its author (``inbox`` suppresses it for group rows).
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.conversations.groups import (
    MEMBER_USER,
    GroupEntry,
    GroupState,
    fold_group,
    wake_members,
)
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import parse_agent_uri

from .requests import group_log

ROW_WINDOW_MS = 86_400_000
ROW_GROUP_MESSAGE = "groupMessageId"


def group_row_id(group_id: str, entry_id: str, member_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hyprial:group-row:{group_id}:{entry_id}:{member_id}"))


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    try:
        app._log(level, "conversations", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks delivery
        pass


def _local_agent(app: Any, member_id: str) -> Any:
    try:
        agent = app.agents.get(member_id)
    except Exception:  # noqa: BLE001 - an unknown id names no local agent
        return None
    parsed = None if agent is None else parse_agent_uri(str(agent.uri))
    if parsed is None or parsed[0] != str(app.owner) or parsed[1] != str(app.node_id):
        return None
    return agent


def _row(entry: GroupEntry, member_id: str, sender: str, recipient: str) -> InboxMessage:
    payload: dict[str, Any] = {
        "message": entry.body.decode("utf-8", errors="replace"),
        ROW_GROUP_MESSAGE: entry.entry_id,
    }
    if entry.mentions:
        payload["mentions"] = list(entry.mentions)
    if entry.reply_to is not None:
        payload["replyTo"] = entry.reply_to.to_json()
    group_id = entry.conversation_id
    return InboxMessage(
        message_id=group_row_id(group_id, entry.entry_id, member_id),
        conversation_id=group_id,
        sender=sender,
        recipient=recipient,
        payload=json.dumps(payload, separators=(",", ":")).encode(),
        intent=entry.intent or "request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=f"group:{group_id}:{entry.entry_id}:{member_id}",
        created_at_ms=entry.created_at_ms,
        summary=entry.summary,
    )


def deliver_entry(app: Any, entry: GroupEntry, state: GroupState, *, wake: bool) -> int:
    """Deliver *entry* to the local agent members it wakes; the rows accepted."""

    now = time.time_ns() // 1_000_000
    if entry.system is not None or entry.recorded_at_ms <= now - ROW_WINDOW_MS:
        return 0
    author = state.member(entry.sender)
    transport = getattr(app, "_local_delivery", None)
    if author is None or transport is None:
        return 0
    sender = author.member_id if author.kind == MEMBER_USER else (
        entry.from_address or author.address or author.member_id)
    delivered = 0
    for member_id in wake_members(entry, state.members):
        member = state.member(member_id)
        agent = _local_agent(app, member_id)
        if member is None or member.joined_at_ms > entry.created_at_ms or agent is None:
            continue
        row = _row(entry, member_id, sender, str(agent.uri))
        try:
            accepted = bool(transport.deliver(row))
        except Exception as error:  # noqa: BLE001 - the next start delivers it again
            accepted = False
            _log(app, "warn", "conversation.group_row_failed", conversationId=entry.conversation_id,
                 entryId=entry.entry_id, messageId=row.message_id, errorType=type(error).__name__)
        if not accepted:
            continue
        delivered += 1
        if wake:
            try:
                app._wake_dormant_agent(agent.actor, reason="pending-work")
            except Exception as error:  # noqa: BLE001 - restore and the scan wake it later
                _log(app, "warn", "conversation.group_wake_failed", actor=str(agent.uri),
                     errorType=type(error).__name__)
    return delivered


def deliver_written(app: Any, entry: GroupEntry, entries: tuple[GroupEntry, ...]) -> None:
    """The kick after a local write of *entry*; never raises."""

    try:
        deliver_entry(app, entry, fold_group(entries), wake=True)
    except Exception as error:  # noqa: BLE001 - the write stands; the next start delivers
        _log(app, "warn", "conversation.group_rows_failed", conversationId=entry.conversation_id,
             entryId=entry.entry_id, errorType=type(error).__name__)


def deliver_recent(app: Any) -> int:
    """The restart pass: every group's entries inside the window; the rows accepted.

    Raises when the group documents cannot be read now (the caller retries).
    """

    if getattr(app, "_local_delivery", None) is None:
        raise RuntimeError("the local delivery transport is not composed yet")
    log = group_log(app)
    delivered = 0
    for group_id in log.groups():
        entries = log.read(group_id)
        state = fold_group(entries)
        for entry in entries:
            delivered += deliver_entry(app, entry, state, wake=False)
    return delivered


__all__ = [
    "ROW_GROUP_MESSAGE",
    "ROW_WINDOW_MS",
    "deliver_entry",
    "deliver_recent",
    "deliver_written",
    "group_row_id",
]
