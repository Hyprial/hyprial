"""Group conversation entries, members and the membership fold (slice GC1).

Decision notes (group chat design draft §2, owner decisions §10, 2026-11-18):

- A group is one document of entries keyed by ``entryId`` (a message id for
  a message, a minted operation id for a system entry), not one entry per
  recipient.  Line format ``hyprial-group-record/v1``.  A line with another
  format, a missing required field or a required field of the wrong shape is
  rejected whole; there are no partial entries.  Unknown fields are ignored
  so a later writer may add optional fields.
- There is no ``hops`` field (hop cap rejected, §10 8.5).  System events are
  ``created``, ``member-added``, ``member-removed`` and ``member-left`` only;
  a line naming any other event is rejected.  No rename this round.
- ``system`` always carries ``memberId``; ``created`` and ``member-added``
  also carry the full member record, which must name the same member, and
  ``created`` the title.  System entries carry no body.
- Member ids carry no machine segment: ``user:<owner>`` for the human (any
  machine's name-form user agent address canonicalises to it by the
  user-agent predicate; this machine's instance reaches it through the
  member resolver's registry check, UA4), ``a.<id>`` for an id-form agent.  A name-form agent address
  has no stable id and is refused.  ``kind`` is decided here, once, by that
  predicate, never by comparing addresses.
- The member list and the title are a pure fold of the system entries,
  ordered by ``(createdAtMs, entryId)`` after dropping repeated entry ids, so
  every reader of the same entry set gets the same state, whatever order
  the writers' segments were read in.  The first ``created`` wins; a
  membership entry is applied only when its author is entitled at that
  point: add and remove need a current manager, leave needs the member
  itself, and a manager is never removed and never leaves (owner-only
  management, §10 8.6).  Anything else, including entries ordered before
  ``created`` (clock skew between writers) and entries from a removed
  member, changes nothing.  Messages never change the state.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.conversations.ids import endpoint_key
from hyprial.daemon.impl.user_agent.local_target import canonical_user_end
from hyprial.kernel import is_agent_id_segment, is_group_id_segment, parse_user_uri

GROUP_RECORD_FORMAT = "hyprial-group-record/v1"
ENTRY_KIND = "message"
CLASS_INTERACTION = "interaction"
CLASS_SYSTEM = "system"
MEMBER_USER = "user"
MEMBER_AGENT = "agent"
EVENT_CREATED = "created"
EVENT_MEMBER_ADDED = "member-added"
EVENT_MEMBER_REMOVED = "member-removed"
EVENT_MEMBER_LEFT = "member-left"
SYSTEM_EVENTS = frozenset({EVENT_CREATED, EVENT_MEMBER_ADDED, EVENT_MEMBER_REMOVED,
                           EVENT_MEMBER_LEFT})
INTENTS = frozenset({"request", "reply"})
_HEX = frozenset("0123456789abcdef")


class _Invalid(ValueError):
    pass


def member_id_for(address: str) -> tuple[str, str]:
    """The member id and kind of *address*; ``ValueError`` when it has no stable id."""

    if not isinstance(address, str) or not address:
        raise ValueError("a member needs an address")
    key = endpoint_key(canonical_user_end(address))
    if parse_user_uri(key) is not None:
        return key, MEMBER_USER
    if is_agent_id_segment(key):
        return key, MEMBER_AGENT
    raise ValueError(f"{address!r} has no stable member id: use the agent's id-form address")


@dataclass(frozen=True)
class GroupMember:
    """One member record, as it was when the member was added."""

    member_id: str
    kind: str
    display_name: str
    manager: bool
    joined_at_ms: int
    added_by: str
    address: str | None = None

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "memberId": self.member_id,
            "kind": self.kind,
            "displayName": self.display_name,
            "manager": self.manager,
            "joinedAtMs": self.joined_at_ms,
            "addedBy": self.added_by,
        }
        if self.address is not None:
            document["address"] = self.address
        return document

    @classmethod
    def from_json(cls, document: object) -> GroupMember:
        record = _mapping(document)
        member_id = _text(record, "memberId")
        kind = _text(record, "kind")
        if member_id_for(member_id) != (member_id, kind):
            raise _Invalid("memberId")
        return cls(
            member_id=member_id,
            kind=kind,
            display_name=_text(record, "displayName", empty=True),
            manager=_flag(record, "manager"),
            joined_at_ms=_integer(record, "joinedAtMs"),
            added_by=_text(record, "addedBy"),
            address=_optional_text(record, "address"),
        )


@dataclass(frozen=True)
class ReplyTo:
    """The entry a message replies to, captured by the sender's daemon."""

    message_id: str
    sender: str
    excerpt: str

    def to_json(self) -> dict[str, Any]:
        return {"messageId": self.message_id, "from": self.sender, "excerpt": self.excerpt}

    @classmethod
    def from_json(cls, document: object) -> ReplyTo:
        record = _mapping(document)
        return cls(
            message_id=_text(record, "messageId"),
            sender=_text(record, "from"),
            excerpt=_text(record, "excerpt", empty=True),
        )


@dataclass(frozen=True)
class SystemEvent:
    """A membership change or the group's creation."""

    event: str
    member_id: str
    member: GroupMember | None = None
    title: str | None = None

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"event": self.event, "memberId": self.member_id}
        if self.member is not None:
            document["member"] = self.member.to_json()
        if self.title is not None:
            document["title"] = self.title
        return document

    @classmethod
    def from_json(cls, document: object) -> SystemEvent:
        record = _mapping(document)
        event = _text(record, "event")
        if event not in SYSTEM_EVENTS:
            raise _Invalid("event")
        member_id = _text(record, "memberId")
        member = None
        title = None
        if event in (EVENT_CREATED, EVENT_MEMBER_ADDED):
            member = GroupMember.from_json(record.get("member"))
            if member.member_id != member_id:
                raise _Invalid("member")
        if event == EVENT_CREATED:
            title = _text(record, "title")
        return cls(event=event, member_id=member_id, member=member, title=title)


@dataclass(frozen=True)
class GroupEntry:
    """One line of a group document."""

    entry_id: str
    conversation_id: str
    message_class: str
    sender: str
    created_at_ms: int
    recorded_at_ms: int
    writer: str
    from_address: str | None = None
    intent: str | None = None
    summary: str = ""
    body: bytes = b""
    body_omitted: str | None = None
    payload_sha256: str | None = None
    reply_to: ReplyTo | None = None
    mentions: tuple[str, ...] = ()
    operation_key_sha256: str | None = None
    system: SystemEvent | None = None

    @property
    def key(self) -> str:
        return self.entry_id

    @property
    def order(self) -> tuple[int, str]:
        return self.created_at_ms, self.entry_id

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "format": GROUP_RECORD_FORMAT,
            "entryId": self.entry_id,
            "conversationId": self.conversation_id,
            "kind": ENTRY_KIND,
            "messageClass": self.message_class,
            "from": self.sender,
            "createdAtMs": self.created_at_ms,
            "recordedAtMs": self.recorded_at_ms,
            "writer": self.writer,
        }
        optional = {
            "fromAddress": self.from_address,
            "intent": self.intent,
            "payloadSha256": self.payload_sha256,
            "operationKeySha256": self.operation_key_sha256,
        }
        document.update({name: value for name, value in optional.items() if value is not None})
        if self.system is not None:
            document["system"] = self.system.to_json()
        else:
            document["summary"] = self.summary
            if self.body_omitted is None:
                document["body"] = base64.b64encode(self.body).decode("ascii")
            else:
                document["bodyOmitted"] = self.body_omitted
        if self.reply_to is not None:
            document["replyTo"] = self.reply_to.to_json()
        if self.mentions:
            document["mentions"] = list(self.mentions)
        return document

    def to_line(self) -> str:
        return json.dumps(self.to_json(), separators=(",", ":"), sort_keys=True) + "\n"

    @classmethod
    def from_line(cls, line: str) -> GroupEntry | None:
        """The entry one line holds, or None for anything else."""

        try:
            document = json.loads(line)
            return cls._from_json(_mapping(document))
        except ValueError:
            return None

    @classmethod
    def _from_json(cls, document: Mapping[str, Any]) -> GroupEntry:
        if document.get("format") != GROUP_RECORD_FORMAT:
            raise _Invalid("format")
        if document.get("kind") != ENTRY_KIND:
            raise _Invalid("kind")
        conversation_id = _text(document, "conversationId")
        if not is_group_id_segment(conversation_id):
            raise _Invalid("conversationId")
        message_class = _text(document, "messageClass")
        fields: dict[str, Any] = {
            "entry_id": _text(document, "entryId"),
            "conversation_id": conversation_id,
            "message_class": message_class,
            "sender": _text(document, "from"),
            "created_at_ms": _integer(document, "createdAtMs"),
            "recorded_at_ms": _integer(document, "recordedAtMs"),
            "writer": _text(document, "writer"),
            "from_address": _optional_text(document, "fromAddress"),
            "operation_key_sha256": _optional_text(document, "operationKeySha256"),
        }
        if "replyTo" in document:
            fields["reply_to"] = ReplyTo.from_json(document["replyTo"])
        if "mentions" in document:
            mentions = document["mentions"]
            if not isinstance(mentions, list) or not all(
                isinstance(item, str) and item for item in mentions
            ):
                raise _Invalid("mentions")
            fields["mentions"] = tuple(mentions)
        if message_class == CLASS_SYSTEM:
            if "body" in document or "bodyOmitted" in document:
                raise _Invalid("body")
            fields["system"] = SystemEvent.from_json(document.get("system"))
            fields["intent"] = _optional_text(document, "intent")
            fields["payload_sha256"] = _optional_text(document, "payloadSha256")
            return cls(**fields)
        if message_class != CLASS_INTERACTION or "system" in document:
            raise _Invalid("messageClass")
        intent = _text(document, "intent")
        if intent not in INTENTS:
            raise _Invalid("intent")
        digest = _text(document, "payloadSha256")
        if len(digest) != 64 or not set(digest) <= _HEX:
            raise _Invalid("payloadSha256")
        fields.update(intent=intent, payload_sha256=digest,
                      summary=_text(document, "summary", empty=True))
        if "bodyOmitted" in document:
            if "body" in document:
                raise _Invalid("body")
            fields["body_omitted"] = _text(document, "bodyOmitted")
        else:
            try:
                fields["body"] = base64.b64decode(
                    _text(document, "body", empty=True).encode("ascii"), validate=True
                )
            except (UnicodeEncodeError, binascii.Error) as error:
                raise _Invalid("body") from error
        return cls(**fields)


@dataclass(frozen=True)
class GroupState:
    """Title, creator and current members: the fold of a group's entries."""

    title: str | None = None
    created_by: str | None = None
    created_at_ms: int | None = None
    members: tuple[GroupMember, ...] = ()

    def member(self, member_id: str) -> GroupMember | None:
        for member in self.members:
            if member.member_id == member_id:
                return member
        return None


def ordered(entries: Iterable[GroupEntry]) -> tuple[GroupEntry, ...]:
    """*entries* by ``(createdAtMs, entryId)``, one per entry id."""

    unique: dict[str, GroupEntry] = {}
    for entry in sorted(entries, key=lambda item: (*item.order, item.to_line())):
        unique.setdefault(entry.entry_id, entry)
    return tuple(unique.values())


def fold_group(entries: Iterable[GroupEntry]) -> GroupState:
    """The group state *entries* describe; pure and order independent."""

    title: str | None = None
    created_by: str | None = None
    created_at_ms: int | None = None
    members: dict[str, GroupMember] = {}
    for entry in ordered(entries):
        event = entry.system
        if event is None:
            continue
        author = members.get(entry.sender)
        target = members.get(event.member_id)
        if event.event == EVENT_CREATED:
            if created_by is None and event.member is not None:
                title, created_by, created_at_ms = event.title, entry.sender, entry.created_at_ms
                members[event.member_id] = event.member
        elif event.event == EVENT_MEMBER_ADDED:
            if author is not None and author.manager and target is None and event.member:
                members[event.member_id] = event.member
        elif target is None or target.manager:
            continue
        elif (event.event == EVENT_MEMBER_REMOVED and author is not None and author.manager) or (
            event.event == EVENT_MEMBER_LEFT and entry.sender == event.member_id
        ):
            del members[event.member_id]
    return GroupState(title, created_by, created_at_ms, tuple(members.values()))


def _mapping(document: object) -> Mapping[str, Any]:
    if not isinstance(document, dict):
        raise _Invalid("object")
    return document


def _text(document: Mapping[str, Any], name: str, *, empty: bool = False) -> str:
    value = document.get(name)
    if not isinstance(value, str) or (not empty and not value):
        raise _Invalid(name)
    return value


def _optional_text(document: Mapping[str, Any], name: str) -> str | None:
    return _text(document, name) if name in document else None


def _integer(document: Mapping[str, Any], name: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid(name)
    return value


def _flag(document: Mapping[str, Any], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise _Invalid(name)
    return value


__all__ = [
    "CLASS_INTERACTION",
    "CLASS_SYSTEM",
    "EVENT_CREATED",
    "EVENT_MEMBER_ADDED",
    "EVENT_MEMBER_LEFT",
    "EVENT_MEMBER_REMOVED",
    "GROUP_RECORD_FORMAT",
    "MEMBER_AGENT",
    "MEMBER_USER",
    "SYSTEM_EVENTS",
    "GroupEntry",
    "GroupMember",
    "GroupState",
    "ReplyTo",
    "SystemEvent",
    "fold_group",
    "member_id_for",
    "ordered",
]
