"""Harness for the group delivery tests (slice GC4).

The daemon is GC3's group daemon (golden daemon, id-form agents alpha,
beta, gamma, the owner ``user:op``, an isolated ``LocalOrgFs`` placement,
link bypass on unless a test builds it otherwise).  Group messages are
written with ``message.send`` / ``message.reply`` through ``app.handle``;
what a member agent then holds is read from its inbox, its
``message.pending.batch`` and the restore predicate the daemon wakes a
dormant agent by.

``sweep`` is the restart catch-up pass, reached through the
``inbox_surface.groups`` package; ``write_unannounced`` appends a message
entry straight to the document, as a daemon that stopped between writing
the entry and creating its rows leaves it.
"""

from __future__ import annotations

import hashlib
import importlib
import time
from typing import Any

from hyprial.daemon.impl.conversations.groups import CLASS_INTERACTION, GroupEntry
from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    GroupDaemon,
    group_daemon,
    handle,
    make_group,
    messages,
    reply,
    send,
)
from tests.support.golden.app import SESSIONS

GROUPS_PACKAGE = "hyprial.daemon.impl.application.messaging.inbox_surface.groups"


def linked_group_daemon(golden_daemon: Any, monkeypatch: Any) -> GroupDaemon:
    """GC3's group daemon with link governance on (no bypass)."""

    from types import SimpleNamespace

    from hyprial.daemon.impl.composition.conversations import compose_conversation_recorder
    from hyprial.daemon.impl.orgfs import LocalOrgFs
    from tests.daemon.conversations.conversation_support import FlakyOrgFs

    daemon = golden_daemon(id_form=True, user_agent=True, bypass=False)
    fs = FlakyOrgFs(LocalOrgFs(author=OWNER))
    monkeypatch.setattr(daemon.app, "_require_orgfs_runtime",
                        lambda: SimpleNamespace(facade=fs), raising=False)
    compose_conversation_recorder(daemon.app, fs=fs, start=False, clock_ms=daemon.clock.ms)
    return GroupDaemon(daemon, fs)


def group_rows(booted: GroupDaemon, who: str, group: str | None = None) -> list[Any]:
    """The pending rows of agent *who* that carry a group conversation id."""

    rows = booted.app._inbox.pending_messages(booted.address(who))
    return [
        row for row in rows
        if row.conversation_id.startswith("g.")
        and (group is None or row.conversation_id == group)
    ]


def row_ids(booted: GroupDaemon, who: str, group: str | None = None) -> list[str]:
    return [row.message_id for row in group_rows(booted, who, group)]


def woken(booted: GroupDaemon, who: str) -> bool:
    """Whether the daemon's dormant-agent wake would pick *who* up."""

    agent = booted.app.agents.require(who)
    return bool(booted.app._pending_restore_work(agent))


def batch(booted: GroupDaemon, who: str) -> dict[str, Any]:
    return booted.app.handle(
        "message.pending.batch",
        {"from": booted.address(who), "sessionRef": SESSIONS[who]},
        _trusted_message_origin="ipc",
    )


def sweep(booted: GroupDaemon) -> Any:
    """Run the restart catch-up pass once; fails as an assertion while it is missing."""

    package = importlib.import_module(GROUPS_PACKAGE)
    deliver_recent = getattr(package, "deliver_recent", None)
    assert callable(deliver_recent), "inbox_surface.groups has no deliver_recent pass"
    return deliver_recent(booted.app)


def write_unannounced(booted: GroupDaemon, group: str, text: str, *, entry_id: str,
                      at_ms: int, mentions: tuple[str, ...] = ()) -> GroupEntry:
    """Append an owner message entry to the document without creating any row."""

    from hyprial.daemon.impl.application.messaging.inbox_surface.groups.requests import (
        group_log,
    )

    body = text.encode()
    entry = GroupEntry(
        entry_id=entry_id,
        conversation_id=group,
        message_class=CLASS_INTERACTION,
        sender=OWNER,
        created_at_ms=at_ms,
        recorded_at_ms=time.time_ns() // 1_000_000,
        writer=str(booted.app.node_id),
        from_address=OWNER,
        intent="request",
        summary=text[:80],
        body=body,
        payload_sha256=hashlib.sha256(body).hexdigest(),
        mentions=mentions,
    )
    group_log(booted.app).append(group, [entry])
    return entry


__all__ = [
    "OWNER",
    "GroupDaemon",
    "batch",
    "group_daemon",
    "group_rows",
    "handle",
    "linked_group_daemon",
    "make_group",
    "messages",
    "reply",
    "row_ids",
    "send",
    "sweep",
    "woken",
    "write_unannounced",
]
