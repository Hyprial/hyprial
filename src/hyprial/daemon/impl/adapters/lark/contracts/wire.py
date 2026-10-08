"""Platform-neutral seams and values used by the Lark adapter."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
MAX_RUNTIME_TARGET_FIELD_BYTES = 4096
MAX_RUNTIME_TARGET_ITEM_BYTES = 4096
def encode_lark_text_content(text: str) -> str:
    """Encode the exact UTF-8 JSON string handed to the generated SDK."""

    return json.dumps({"text": text}, ensure_ascii=False)


def is_safe_runtime_target_field(value: object) -> bool:
    """Reject target data that could forge UI lines or exhaust a reply."""

    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > MAX_RUNTIME_TARGET_FIELD_BYTES:
            return False
    except UnicodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


@dataclass(frozen=True, slots=True)
class ActorTarget:
    actor_id: str
    actor_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    """Legacy presence address, not a stable Agent entity."""

    target_uri: str
    actor: str
    alias: str
    status: str

    def as_actor_target(self) -> ActorTarget:
        return ActorTarget(
            actor_id=self.target_uri,
            actor_key=self.actor,
            display_name=self.target_uri,
        )


@dataclass(frozen=True, slots=True)
class AgentDirectoryEntry:
    """The Lark-facing projection of one daemon agent-directory row."""

    name: str
    status: str
    pinned_adapters: tuple[str, ...] = ()
    preferred_harness: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    message_id: str
    from_actor_id: str
    to: ActorTarget
    text: str
    conversation_id: str
    provider_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HarnessReceipt:
    message_id: str


class HarnessSubmissionRejected(RuntimeError):
    """An authoritative single-target result explicitly refused submission."""


@dataclass(frozen=True, slots=True)
class HarnessDelivery:
    delivery_id: str
    message_id: str
    reply_to: str
    from_actor: ActorTarget
    text: str


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    message_id: str
    sender_id: str
    sender_type: str
    text: str
    #: Defaulted so existing constructions stay valid; quoting is no longer
    #: restricted to ``text``, so the rendered header must not claim it is.
    message_type: str = "text"


@dataclass(frozen=True, slots=True)
class InboundOutcome:
    status: str
    harness_message_id: str | None = None
    target_actor_id: str | None = None
    ack_error: str | None = None
    new_dead_letter: bool = False
    pending_capacity: PendingCommandCapacityStatus | None = None


@dataclass(frozen=True, slots=True)
class PendingCommandCapacityStatus:
    """Secret-free status for the pending-response map's aggregate budget.

    This does not describe or cap the complete Lark state file; correlation,
    recovery, and audit maps have separate retention rules.
    """

    status: str
    count: int
    bytes: int
    max_count: int
    max_bytes: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "status": self.status,
            "count": self.count,
            "bytes": self.bytes,
            "maxCount": self.max_count,
            "maxBytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: str
    native_message_id: str | None = None
    error: str | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Outcome of one post-reconnect history reconciliation sweep.

    Failures are split by *whether retrying could ever clear them*, because
    the caller fails closed on them and a fail-closed gate whose condition can
    never clear is not a gate -- it is a restart loop.

    ``retryable_errors`` is everything a later sweep might get past: a
    timeout, a rate limit, a truncated page run.  ``blocked_chats`` is the
    other kind: this app is not permitted to read that chat, so every sweep
    from now until someone grants a scope will fail identically.
    """

    chats_scanned: int
    messages_scanned: int
    forwarded: int
    duplicates: int
    dead_lettered: int
    retryable_errors: tuple[str, ...] = field(default_factory=tuple)
    blocked_chats: tuple[str, ...] = field(default_factory=tuple)
    #: Chats the platform permanently refused this sweep (e.g. disbanded
    #: group): retired out of the scan set, not counted as sweep errors.
    retired_chats: tuple[str, ...] = field(default_factory=tuple)
    pending_attempted: int = 0
    pending_remaining: int = 0
    pending_held: int = 0


@dataclass(frozen=True, slots=True)
class LarkInboundMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    text: str
    sender_id: str
    sender_type: str
    root_id: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    create_time: str | None = None
    message_type: str = "text"
    content_status: str = "supported"
    #: The sender's cross-App union_id when the delivery path carries one
    #: (live websocket events do; REST recovery models do not).  open_ids are
    #: namespaced per App, so this is the only cross-adapter identity join.
    sender_union_id: str | None = None
    #: Display names of @-mentions, for humans and logs only.  Routing never
    #: matches on a name: names are mutable and collide.
    mentions: tuple[str, ...] = field(default_factory=tuple)
    #: The open_ids behind those mentions.  This is the identity key the
    #: adapter compares against its own bot open_id to decide "was THIS bot
    #: addressed" (bot-mention routing to the pinned agent).
    mention_open_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conversation_id(self) -> str:
        # A direct-message pin belongs to the whole DM. Root/thread ids are
        # delivery metadata and must not split the conversation identity.
        return f"lark:{self.chat_id}:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class LarkBotInfo:
    """This app's own bot identity (``bot/v3/info``).

    ``open_id`` is the bot's presence id in *this App's* namespace;
    ``app_name`` is the display name the bot bears in chats.
    """

    open_id: str
    app_name: str | None = None


@dataclass(frozen=True, slots=True)
class LarkChatSummary:
    """One row of the group list this app belongs to (``im/v1/chats``)."""

    chat_id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class LarkChatMember:
    """One member row of a group (``im/v1/chats/{chat_id}/members``)."""

    open_id: str
    name: str | None = None


class LarkHistoryBatch(tuple[LarkInboundMessage, ...]):
    """Tuple-compatible history result with a bounded-completeness signal."""

    complete: bool

    def __new__(
        cls,
        values: tuple[LarkInboundMessage, ...] | list[LarkInboundMessage] = (),
        *,
        complete: bool = True,
    ) -> LarkHistoryBatch:
        instance = super().__new__(cls, values)
        instance.complete = complete
        return instance


@runtime_checkable
class LarkApiPort(Protocol):
    def add_reaction(self, message_id: str, emoji_type: str) -> None: ...

    def bot_open_id(self) -> str | None: ...

    def clear_reaction(self, message_id: str, emoji_type: str) -> None: ...

    def reply(
        self, message_id: str, text: str, *, idempotency_key: str
    ) -> str: ...

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str: ...

    def get_message(self, message_id: str) -> QuotedMessage | None: ...

    def get_inbound_message(
        self, message_id: str, *, chat_type: str | None = None
    ) -> LarkInboundMessage | None: ...

    def list_chat_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 50,
        max_pages: int = 20,
        chat_type: str | None = None,
    ) -> LarkHistoryBatch: ...


@runtime_checkable
class HarnessPort(Protocol):
    def send_request(self, request: HarnessRequest, *, timeout: float | None = None) -> HarnessReceipt: ...

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None: ...

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None: ...


@runtime_checkable
class RouteDirectory(Protocol):
    """Routing lookups the adapter needs from the worker.

    ``resolve_mention`` is gone: it matched mention display names against
    route names and returned the route's native chat id where an actor id
    belongs -- a TS-era leftover that could only misroute.  Mention routing
    is now identity-based inside the adapter (bot open_id -> pinned agent).
    """

    def actor_by_key(self, actor_key: str) -> ActorTarget | None: ...

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None: ...

    def list_runtime_targets(self) -> tuple[RuntimeTarget, ...]: ...

    def list_agent_directory(self) -> tuple[AgentDirectoryEntry, ...]: ...

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]: ...
