"""Harness for the group message tests (slice GC3).

The daemon is GC2's group daemon (golden daemon, id-form agents alpha, beta,
gamma, the owner ``user:op``, an isolated ``LocalOrgFs`` placement).  Every
call enters through ``app.handle`` with the IPC origin, so the production
registry validates params, declared error codes and results.

``deliver_row`` stands in for GC4's group intake: it puts one pending row
for a group message into an agent's inbox, shaped as draft section 4.3 says
(the group id as ``conversation_id``, the group message id in the payload
under ``groupMessageId``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from tests.daemon.conversations.group_requests.group_request_support import (
    OWNER,
    GroupDaemon,
    group_daemon,
)
from tests.support.golden.app import SESSIONS

ROW_GROUP_MESSAGE = "groupMessageId"


def handle(booted: GroupDaemon, method: str, who: str = OWNER, **params: Any) -> dict[str, Any]:
    return booted.app.handle(
        method, {**booted.caller(who), **params}, _trusted_message_origin="ipc"
    )


def refused(booted: GroupDaemon, method: str, who: str = OWNER, **params: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        handle(booted, method, who, **params)
    return raised.value


def make_group(booted: GroupDaemon, members: tuple[str, ...] = ("alpha", "beta"),
               *, key: str = "gc3-group", title: str = "release") -> str:
    answer = handle(booted, "conversations.create", title=title, members=list(members),
                    idempotencyKey=key)
    return str(answer["conversationId"])


def send_params(group: str, text: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"conversationId": group, "message": text, "summary": text[:80],
            "idempotencyKey": key, **extra}


def send(booted: GroupDaemon, group: str, text: str, who: str = OWNER, *, key: str,
         **extra: Any) -> dict[str, Any]:
    return handle(booted, "message.send", who, **send_params(group, text, key, **extra))


def history(booted: GroupDaemon, group: str, who: str = OWNER, **params: Any) -> dict[str, Any]:
    return handle(booted, "conversations.history", who, conversationId=group, **params)


def messages(booted: GroupDaemon, group: str, who: str = OWNER) -> list[dict[str, Any]]:
    """Every interaction entry of the group, oldest first."""

    entries = history(booted, group, who, limit=200)["entries"]
    return [entry for entry in entries if entry["messageClass"] == "interaction"]


def deliver_row(booted: GroupDaemon, group: str, group_message: str | None, recipient: str,
                *, row_id: str, sender: str = OWNER, text: str = "please cut the release",
                created_at_ms: int = 1_800_000_000_500) -> InboxMessage:
    """One pending group row for *recipient* (an agent name), as GC4 will write it."""

    payload: dict[str, Any] = {"message": text}
    if group_message is not None:
        payload[ROW_GROUP_MESSAGE] = group_message
    row = InboxMessage(
        message_id=row_id,
        conversation_id=group,
        sender=sender,
        recipient=booted.address(recipient),
        payload=json.dumps(payload).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key=f"group:{group}:{row_id}",
        created_at_ms=created_at_ms,
        summary=text[:80],
    )
    assert booted.app._inbox.receive(row).accepted
    return row


def pending_ids(booted: GroupDaemon, who: str) -> list[str]:
    key = OWNER if who == OWNER else booted.address(who)
    return [row.message_id for row in booted.app._inbox.pending_messages(key)]


def reply(
    booted: GroupDaemon, who: str, row_id: str, text: str, **extra: Any
) -> dict[str, Any]:
    return booted.app.handle(
        "message.reply",
        {"from": booted.address(who), "sessionRef": SESSIONS[who], "messageId": row_id,
         "message": text, "summary": text[:80], **extra},
        _trusted_message_origin="ipc",
    )


def reply_refused(
    booted: GroupDaemon, who: str, row_id: str, text: str, **extra: Any
) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        reply(booted, who, row_id, text, **extra)
    return raised.value


__all__ = [
    "OWNER",
    "ROW_GROUP_MESSAGE",
    "GroupDaemon",
    "deliver_row",
    "group_daemon",
    "handle",
    "history",
    "make_group",
    "messages",
    "pending_ids",
    "refused",
    "reply",
    "reply_refused",
    "send",
    "send_params",
]
