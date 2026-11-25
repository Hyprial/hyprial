"""Group sends and group replies: one entry in the group document (slice GC3).

Decision notes (group chat design draft §3.6, §3.7; owner decisions §10;
brief GC3):

- ``message.send`` with a ``g.`` ``conversationId`` is a group send.  It
  appends ONE interaction entry to the group document, keyed by its message
  id, whoever the members are, and queues nothing: no inbox or outbox row,
  no sender copy, no send-link plan, no dispatch gate.  Delivery to members
  and their wake are GC4's group intake, which reads the document.
- ``to`` is omitted or empty for a group send; a non-empty ``to`` with a
  group id is ``INVALID_ARGUMENT``, so no caller believes it addressed a
  subset.  The fields that only mean something for a 1:1 delivery
  (``_DIRECT_ONLY``: attachments, dispatch role and gates, relay, topic and
  an adapter's metadata) are refused with a group id rather than dropped.  ``mentions`` is accepted only with a group id.
- The author is ``member_id_for`` of the caller's stored sender, the rule a
  direct send stores: ``onBehalfOf`` does not move it, so the owner acting
  for an agent writes as ``user:<owner>``.  The author must be a current
  member (``CONVERSATION_NOT_MEMBER``); a ``g.`` id whose document holds no
  ``created`` entry is ``CONVERSATION_NOT_FOUND``.
- Message id: ``uuid5(NAMESPACE_URL, "hyprial:send:<operationId>:<g.id>:0")``,
  the agent-target formula with the group id as target.  The same key from
  the same author is a replay: nothing is written and the one delivery says
  ``duplicate: true``.  It is recognised before the membership check, so a
  member removed since may still replay.  The same key from another author
  names a message that is not the caller's and is ``INVALID_ARGUMENT``.
- ``mentions`` are member ids of current members, each once.  ``replyTo``
  names an interaction entry of this group; ``replyTo.from`` and the
  excerpt (the first 120 characters of its text) are copied into the new
  entry from this daemon's read of the document.
- ``createdAtMs`` is the clock, but never earlier than one millisecond
  after the newest entry read (GC2's stamp), so on one machine an answer is
  always ordered after what it answers.  The body is the UTF-8 text and
  ``payloadSha256`` its digest.  An entry whose line would exceed the
  record's ``ENTRY_MAX_BYTES`` is refused: the document is the only copy.
- The answer is direct send's shape with exactly one delivery whose target
  is the group id; ``accepted`` means the entry was appended and read back,
  ``queued`` is always false.
- A reply to a pending row whose conversation is a group (GC4 writes such
  rows) is a group entry, never a 1:1 message: it replies to the group
  message the row carries under ``ROW_GROUP_MESSAGE`` (``groupMessageId``)
  and its id is ``reply_message_id(<row id>)``, so two members replying to
  one message write two entries.  The caller must still be a member.  A row
  that names no message of the group cannot be answered here
  (``MESSAGE_REPLY_UNAVAILABLE``).  The row is acknowledged by
  ``message.reply`` itself after the entry is written, at the reply path's
  one acknowledgement site; this module never touches the inbox.  A retry
  after the acknowledgement answers the same reply when the document
  already holds it.
- ``append_message`` is the one place a group message entry is written on
  this daemon; slice GC4 delivers the entry it wrote from there
  (``delivery.deliver_written``), after the write and never failing it.
  ``reply_in_group`` computes ``mentions`` via ``_reply_mentions``: unless
  ``no_mention`` is true, it includes the replied-to author when that author
  is a current ``agent`` member distinct from the replier, deduplicated with
  any explicit ``mentions`` (which must name current members via ``_mentions``).
  Wake rules in ``wake.py`` stay untouched and wake only ``entry.mentions``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from hyprial.daemon.impl.conversations.groups import (
    CLASS_INTERACTION,
    MEMBER_AGENT,
    GroupEntry,
    GroupLog,
    GroupState,
    ReplyTo,
    member_id_for,
)
from hyprial.daemon.impl.conversations.record.segments import ENTRY_MAX_BYTES
from hyprial.kernel import DaemonRequestError, ipc_errors, reply_message_id

from .delivery import ROW_GROUP_MESSAGE, deliver_written
from .requests import _existing, _not_member, _stamp, _write, _writer, group_log

EXCERPT_CHARS = 120
_DIRECT_ONLY = ("resourcePaths", "role", "relay", "topic", "providerMetadata",
                "first_output_eta", "human_gates")


def _invalid(message: str) -> DaemonRequestError:
    return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, message)


def _author(sender: str) -> str | None:
    try:
        return member_id_for(sender)[0]
    except ValueError:
        return None


def _mentions(value: object, state: GroupState) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _invalid("mentions must be an array of member ids")
    if len(set(value)) != len(value):
        raise _invalid("mentions names a member twice")
    strangers = [item for item in value if state.member(item) is None]
    if strangers:
        raise _invalid(f"mentions must name current members of the group: {strangers[0]!r}")
    return tuple(value)


def _excerpt(entry: GroupEntry) -> str:
    return entry.body.decode("utf-8", errors="replace")[:EXCERPT_CHARS]


def _replied(entries: tuple[GroupEntry, ...], message_id: str) -> GroupEntry | None:
    return next(
        (entry for entry in entries
         if entry.entry_id == message_id and entry.message_class == CLASS_INTERACTION),
        None,
    )


def append_message(app: Any, log: GroupLog, entry: GroupEntry) -> GroupEntry:
    """Append one message entry and deliver it; the entry as the document now holds it."""

    if len(entry.to_line().encode("utf-8")) > ENTRY_MAX_BYTES:
        raise _invalid("the message is too large for the group document")
    stored = _write(log, entry.conversation_id, [entry])
    written = next((item for item in stored if item.entry_id == entry.entry_id), entry)
    deliver_written(app, written, stored)
    return written


def _message(app: Any, entries: tuple[GroupEntry, ...], *, group_id: str, entry_id: str,
             author: str, sender: str, text: str, summary: str,
             reply_to: GroupEntry | None, mentions: tuple[str, ...]) -> GroupEntry:
    body = text.encode("utf-8")
    return GroupEntry(
        entry_id=entry_id,
        conversation_id=group_id,
        message_class=CLASS_INTERACTION,
        sender=author,
        created_at_ms=_stamp(entries),
        recorded_at_ms=time.time_ns() // 1_000_000,
        writer=_writer(app),
        from_address=sender,
        intent="request" if reply_to is None else "reply",
        summary=summary,
        body=body,
        payload_sha256=hashlib.sha256(body).hexdigest(),
        reply_to=None if reply_to is None else ReplyTo(
            reply_to.entry_id, reply_to.sender, _excerpt(reply_to)),
        mentions=mentions,
    )


def _send_answer(sender: str, operation_id: str, group_id: str, message_id: str,
                 *, duplicate: bool) -> dict[str, Any]:
    delivery: dict[str, Any] = {
        "target": group_id, "messageId": message_id, "conversationId": group_id,
        "accepted": True, "queued": False,
    }
    if duplicate:
        delivery["duplicate"] = True
    return {
        "ok": True,
        "actingPrincipal": sender,
        "operationId": operation_id,
        "conversationId": group_id,
        "messageId": message_id,
        "deliveries": [delivery],
    }


def send_to_group(app: Any, params: dict[str, Any], *, sender: str,
                  summary: str) -> dict[str, Any]:
    """``message.send`` to a group: one entry in its document."""

    group_id = str(params["conversationId"])
    targets = params.get("to")
    if targets is not None and targets != []:
        raise _invalid("a group send takes no to: the members are the group's; "
                       "name members with mentions")
    for field in _DIRECT_ONLY:
        if params.get(field) not in (None, []):
            raise _invalid(f"{field} is not supported for a group conversation")
    text = params.get("message")
    if not isinstance(text, str) or not text:
        raise _invalid("message is required")
    author = _author(sender)
    log = group_log(app)
    entries, state = _existing(log, group_id)
    operation_id = str(params.get("idempotencyKey") or uuid4())
    entry_id = str(uuid5(NAMESPACE_URL, f"hyprial:send:{operation_id}:{group_id}:0"))
    stored = next((entry for entry in entries if entry.entry_id == entry_id), None)
    if stored is not None:
        if stored.sender != author:
            raise _invalid("idempotencyKey already names another sender's message")
        return _send_answer(sender, operation_id, group_id, entry_id, duplicate=True)
    if author is None or state.member(author) is None:
        raise _not_member(group_id)
    mentions = _mentions(params.get("mentions"), state)
    reply_to = None
    if params.get("replyTo") is not None:
        reply_to = _replied(entries, str(params["replyTo"]))
        if reply_to is None:
            raise _invalid("replyTo must name a message of this group")
    entry = _message(app, entries, group_id=group_id, entry_id=entry_id, author=author,
                     sender=sender, text=text, summary=summary, reply_to=reply_to,
                     mentions=mentions)
    append_message(app, log, entry)
    return _send_answer(sender, operation_id, group_id, entry_id, duplicate=False)


def _row_group_message(row: Any) -> str | None:
    try:
        payload = json.loads(row.payload)
    except (TypeError, ValueError):
        return None
    value = payload.get(ROW_GROUP_MESSAGE) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def _unanswerable(message: str) -> DaemonRequestError:
    return DaemonRequestError(ipc_errors.MESSAGE_REPLY_UNAVAILABLE, message)


def _reply_mentions(
    replied: GroupEntry,
    state: GroupState,
    *,
    author: str,
    mentions: object,
    no_mention: bool,
) -> tuple[str, ...]:
    """Mentions for a group reply: the replied-to agent author by default, plus explicit ones."""

    explicit = _mentions(mentions, state)
    default: tuple[str, ...] = ()
    if not no_mention:
        replied_member = state.member(replied.sender)
        if (
            replied_member is not None
            and replied_member.kind == MEMBER_AGENT
            and replied_member.member_id != author
        ):
            default = (replied_member.member_id,)
    return tuple(dict.fromkeys((*default, *explicit)))


def reply_in_group(
    app: Any,
    *,
    sender: str,
    row: Any,
    text: str,
    summary: str,
    pending: bool,
    mentions: object = None,
    no_mention: bool = False,
) -> GroupEntry:
    """The group entry answering the group row *row*; written when *pending*.

    A row that is no longer pending answers only the reply the document
    already holds (a retry after the acknowledgement).
    """

    group_id = str(row.conversation_id)
    log = group_log(app)
    entries, state = _existing(log, group_id)
    entry_id = reply_message_id(row.message_id)
    author = _author(sender)
    stored = next((entry for entry in entries if entry.entry_id == entry_id), None)
    if stored is not None and stored.sender == author:
        return stored
    if not pending:
        raise _unanswerable("pending message was not found")
    if author is None or state.member(author) is None:
        raise _not_member(group_id)
    group_message = _row_group_message(row)
    replied = None if group_message is None else _replied(entries, group_message)
    if replied is None:
        raise _unanswerable("the pending message names no message of its group")
    resolved_mentions = _reply_mentions(
        replied, state, author=author, mentions=mentions, no_mention=no_mention
    )
    entry = _message(app, entries, group_id=group_id, entry_id=entry_id, author=author,
                     sender=sender, text=text, summary=summary, reply_to=replied,
                     mentions=resolved_mentions)
    return append_message(app, log, entry)


def group_reply_answer(row_id: str, entry: GroupEntry, acknowledged: Any) -> dict[str, Any]:
    """``message.reply``'s answer for a group reply; *acknowledged* is the ack result or None."""

    ok = True if acknowledged is None else bool(acknowledged.acknowledged)
    code = None if acknowledged is None else acknowledged.code
    return {
        "ok": ok,
        "messageId": row_id,
        "replyMessageId": entry.entry_id,
        "replied": True,
        "acknowledged": ok,
        "queued": False,
        **({"code": code} if code else {}),
    }


__all__ = [
    "EXCERPT_CHARS",
    "ROW_GROUP_MESSAGE",
    "append_message",
    "group_reply_answer",
    "reply_in_group",
    "send_to_group",
]
