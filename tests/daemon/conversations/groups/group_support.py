"""Builders for the group conversation tests (slice GC1)."""

from __future__ import annotations

import hashlib

from hyprial.daemon.impl.conversations.groups import (
    EVENT_CREATED,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_LEFT,
    EVENT_MEMBER_REMOVED,
    GroupEntry,
    GroupMember,
    ReplyTo,
    SystemEvent,
)

GROUP = "g.groupexampleaaaaaaaaaaaaaa"
OWNER = "user:alice"
WORKER = "a.workeraaaaaaaaaaaaaaaaaaaa"
REVIEWER = "a.revieweraaaaaaaaaaaaaaaaaa"
WRITER = "node-a"


def member(member_id: str, *, at: int = 1, manager: bool = False) -> GroupMember:
    kind = "user" if member_id.startswith("user:") else "agent"
    return GroupMember(
        member_id=member_id,
        kind=kind,
        display_name=member_id.rsplit(".", 1)[-1][:8],
        manager=manager,
        joined_at_ms=at,
        added_by=OWNER,
        address=None if kind == "user" else f"agent:alice:host-1:{member_id}",
    )


def system(
    entry_id: str,
    event: str,
    target: str,
    *,
    at: int,
    author: str = OWNER,
    title: str | None = None,
    writer: str = WRITER,
) -> GroupEntry:
    if event in (EVENT_CREATED, EVENT_MEMBER_ADDED):
        record = member(target, at=at, manager=event == EVENT_CREATED)
    else:
        record = None
    return GroupEntry(
        entry_id=entry_id,
        conversation_id=GROUP,
        message_class="system",
        sender=author,
        created_at_ms=at,
        recorded_at_ms=at,
        writer=writer,
        system=SystemEvent(
            event=event,
            member=record,
            member_id=target,
            title=title,
        ),
    )


def created(at: int = 1, *, title: str = "release", entry_id: str = "e-created") -> GroupEntry:
    return system(entry_id, EVENT_CREATED, OWNER, at=at, title=title)


def added(target: str, *, at: int, entry_id: str | None = None, author: str = OWNER,
          writer: str = WRITER) -> GroupEntry:
    return system(entry_id or f"e-add-{target}-{at}", EVENT_MEMBER_ADDED, target, at=at,
                  author=author, writer=writer)


def removed(target: str, *, at: int, author: str = OWNER) -> GroupEntry:
    return system(f"e-remove-{target}-{at}", EVENT_MEMBER_REMOVED, target, at=at,
                  author=author)


def left(target: str, *, at: int, author: str | None = None) -> GroupEntry:
    return system(f"e-left-{target}-{at}", EVENT_MEMBER_LEFT, target, at=at,
                  author=author or target)


def message(
    entry_id: str,
    author: str,
    *,
    at: int,
    text: str = "hello",
    mentions: tuple[str, ...] = (),
    reply_to: ReplyTo | None = None,
    writer: str = WRITER,
) -> GroupEntry:
    body = text.encode()
    return GroupEntry(
        entry_id=entry_id,
        conversation_id=GROUP,
        message_class="interaction",
        sender=author,
        created_at_ms=at,
        recorded_at_ms=at,
        writer=writer,
        from_address=None if author.startswith("user:") else f"agent:alice:host-1:{author}",
        intent="request" if reply_to is None else "reply",
        summary=text[:20],
        body=body,
        payload_sha256=hashlib.sha256(body).hexdigest(),
        mentions=mentions,
        reply_to=reply_to,
    )


__all__ = [
    "GROUP",
    "OWNER",
    "REVIEWER",
    "WORKER",
    "WRITER",
    "added",
    "created",
    "left",
    "member",
    "message",
    "removed",
    "system",
]
