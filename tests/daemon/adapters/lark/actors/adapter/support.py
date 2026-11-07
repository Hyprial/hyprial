from __future__ import annotations

import json
from pathlib import Path

import pytest
from lark_oapi import EventDispatcherHandler

from hyprial.daemon.impl.adapters.lark import (
    AgentDirectoryEntry,
    ActorTarget,
    HarnessReceipt,
    LarkAdapter,
    LarkForwardedItem,
    LarkStateStore,
    QuotedMessage,
    RuntimeTarget,
    normalize_merge_forward,
)
from hyprial.kernel import Logger


def sdk_event(
    callback,
    *,
    event_id: str,
    message_id: str,
    chat_id: str = "oc_dm",
    chat_type: str = "p2p",
    text: str = "please investigate",
    reply_to: str | None = None,
    mention: str | None = None,
    mention_open_id: str = "ou_agent",
    message_type: str = "text",
    content: str | None = None,
) -> None:
    mentions = []
    if mention is not None:
        mentions.append(
            {
                "key": "@_user_1",
                "id": {"open_id": mention_open_id},
                "name": mention,
                "tenant_key": "tenant",
            }
        )
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1720000000000",
            "token": "token",
            "app_id": "cli_test_app",
            "tenant_key": "tenant",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "union_id": "on_sender",
                    "user_id": "sender",
                    "open_id": "ou_sender",
                },
                "sender_type": "user",
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": message_id,
                "root_id": "om_root",
                "parent_id": reply_to,
                "create_time": "1720000000000",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": (
                    json.dumps({"text": text}) if content is None else content
                ),
                "mentions": mentions,
            },
        },
    }
    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    dispatcher._do_without_validation(json.dumps(payload).encode())


#: The open_id the fake platform reports for the adapter's own bot.
BOT_OPEN_ID = "ou_bot_self"


class FakeLark:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.reactions: set[str] = set()
        self.quotes: dict[str, QuotedMessage] = {}
        self.fail_reply = False
        self.fail_clear = False
        self.reply_id = "om_outbound"
        self.last_reply_text: str | None = None
        self.reply_attempts: list[tuple[str, str]] = []
        self.reply_bodies: list[str] = []
        self.visible_replies: dict[str, str] = {}
        # None simulates a platform that cannot answer the bot's identity.
        self.bot_open_id_value: str | None = BOT_OPEN_ID

    def bot_open_id(self) -> str | None:
        return self.bot_open_id_value

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        self.events.append(f"lark.react:{message_id}:{emoji_type}")
        self.reactions.add(message_id)

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        self.events.append(f"lark.clear:{message_id}:{emoji_type}")
        if self.fail_clear:
            raise RuntimeError("reaction cleanup failed")
        self.reactions.discard(message_id)

    def reply(self, message_id: str, text: str, *, idempotency_key: str) -> str:
        self.events.append(f"lark.reply:{message_id}")
        if self.fail_reply:
            raise RuntimeError("send failed")
        self.reply_attempts.append((message_id, idempotency_key))
        self.reply_bodies.append(text)
        self.visible_replies.setdefault(idempotency_key, text)
        self.last_reply_text = text
        return self.reply_id

    def get_message(self, message_id: str) -> QuotedMessage | None:
        self.events.append(f"lark.get:{message_id}")
        return self.quotes.get(message_id)


class FakeHarness:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests = []
        self.accepted: list[tuple[str, str]] = []
        self.rejected: list[tuple[str, str]] = []

    def send_request(self, request):  # type: ignore[no-untyped-def]
        self.events.append(f"harness.send:{request.message_id}")
        self.requests.append(request)
        return HarnessReceipt(message_id="msg_harness_1")

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        self.events.append(f"harness.accept:{delivery_id}:{native_message_id}")
        self.accepted.append((delivery_id, native_message_id))

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        self.events.append(f"harness.reject:{delivery_id}")
        self.rejected.append((delivery_id, error, deterministic))


class FakeRoutes:
    def __init__(self) -> None:
        self.actors: dict[str, ActorTarget] = {}
        self.mentions: dict[str, ActorTarget] = {}
        self.pins: dict[str, ActorTarget] = {}
        self.runtime_targets: tuple[RuntimeTarget, ...] = ()
        self.agent_entries: tuple[AgentDirectoryEntry, ...] = ()

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return next(
            (actor for actor in self.actors.values() if actor.actor_key == actor_key),
            None,
        )

    def resolve_mention(self, name: str) -> ActorTarget | None:
        return self.mentions.get(name)

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        return self.pins.get(conversation_id)

    def list_runtime_targets(self) -> tuple[RuntimeTarget, ...]:
        return self.runtime_targets

    def list_agent_directory(self) -> tuple[AgentDirectoryEntry, ...]:
        if self.agent_entries:
            return self.agent_entries
        return tuple(
            AgentDirectoryEntry(
                name=target.target_uri,
                status="running" if target.status == "online" else "down",
            )
            for target in self.runtime_targets
        )

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]:
        exact = tuple(
            target.as_actor_target()
            for target in self.runtime_targets
            if token in {target.target_uri, target.actor}
        )
        if exact:
            return exact
        folded = token.casefold()
        return tuple(
            target.as_actor_target()
            for target in self.runtime_targets
            if folded == target.alias.casefold()
        )


@pytest.fixture
def actor() -> ActorTarget:
    return ActorTarget(
        actor_id="agent:alice:codex:main",
        actor_key="alice/codex/main",
        display_name="alice/codex/main",
    )


def make_adapter(
    tmp_path: Path, actor: ActorTarget, *, logger: Logger | None = None
):
    events: list[str] = []
    lark = FakeLark(events)
    harness = FakeHarness(events)
    routes = FakeRoutes()
    routes.actors[actor.actor_id] = actor
    routes.mentions[actor.display_name] = actor
    routes.pins["lark:oc_dm:oc_dm"] = actor
    adapter = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:tenant:bot",
        org_context_path=tmp_path / "org-context.md",
        logger=logger,
    )
    return adapter, lark, harness, routes, events


def runtime_target(
    actor: str = "agent:alice:mac:worker", *, status: str = "online"
) -> RuntimeTarget:
    return RuntimeTarget(
        target_uri=actor,
        actor=actor,
        alias=actor.rsplit(":", 1)[-1],
        status=status,
    )






































































def sdk_event_with_content(
    callback,
    *,
    event_id: str,
    message_id: str,
    message_type: str,
    content: dict,
    chat_id: str = "oc_dm",
    chat_type: str = "p2p",
) -> None:
    """Build a real SDK-parsed ``P2ImMessageReceiveV1`` for a non-text
    ``message_type`` (rich text ``post``, ``merge_forward``, ...).

    Goes through the actual ``lark_oapi`` event dispatcher (like ``sdk_event``
    above) rather than a hand-rolled fake, so ``normalize_sdk_event`` is
    exercised against the real SDK model shape, not a stand-in.
    """

    payload = {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": "1720000000000",
            "token": "token",
            "app_id": "cli_test_app",
            "tenant_key": "tenant",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "union_id": "on_sender",
                    "user_id": "sender",
                    "open_id": "ou_sender",
                },
                "sender_type": "user",
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": message_id,
                "root_id": None,
                "parent_id": None,
                "create_time": "1720000000000",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": json.dumps(content),
                "mentions": [],
            },
        },
    }
    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    dispatcher._do_without_validation(json.dumps(payload).encode())




#: The websocket event for a merge-forward carries *only* this placeholder.
#: Verified against the platform: the forwarded conversation exists solely in
#: the ``im.v1.message.get`` response for the shell's own message id.
MERGE_FORWARD_EVENT_CONTENT = {"content": "Merged and Forwarded Message"}


def forwarded_children(shell_id: str) -> tuple[LarkForwardedItem, ...]:
    """``im.v1.message.get`` items for a forward, in the platform's shape.

    Index 0 is the shell (bare, non-JSON placeholder body); the rest are its
    children, each with its own ``msg_type``, its own real body, and an
    ``upper_message_id`` pointing back at the shell.
    """

    return (
        LarkForwardedItem(
            message_id=shell_id,
            message_type="merge_forward",
            content="Merged and Forwarded Message",
        ),
        LarkForwardedItem(
            message_id="om_child_post",
            message_type="post",
            content=json.dumps(
                {
                    "title": "Incident 42",
                    "content": [[{"tag": "text", "text": "gateway timed out"}]],
                }
            ),
            upper_message_id=shell_id,
        ),
        LarkForwardedItem(
            message_id="om_child_text",
            message_type="text",
            content=json.dumps({"text": "and the Cloudflare token: abc123"}),
            upper_message_id=shell_id,
        ),
        LarkForwardedItem(
            message_id="om_child_image",
            message_type="image",
            content=json.dumps({"image_key": "img_v2_fwd"}),
            upper_message_id=shell_id,
            sender_id="ou_forwarder",
            sender_type="user",
        ),
    )


def forwarded_quote(shell_id: str) -> QuotedMessage:
    """What the real gateway returns for a forward, built by the real
    renderer rather than a hand-written expected string."""

    normalized = normalize_merge_forward(shell_id, forwarded_children(shell_id))
    return QuotedMessage(
        message_id=shell_id,
        sender_id="ou_forwarder",
        sender_type="user",
        text=normalized.text,
        message_type=normalized.message_type,
    )
































# ---------------------------------------------------------------------------
# Bot-mention routing: @ THIS bot goes to the pinned agent, by identity key
# ---------------------------------------------------------------------------


def _pin_group(routes: FakeRoutes, actor: ActorTarget, chat_id: str) -> None:
    routes.pins[f"lark:{chat_id}:{chat_id}"] = actor
















# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'BOT_OPEN_ID',
    'FakeHarness',
    'FakeLark',
    'FakeRoutes',
    'MERGE_FORWARD_EVENT_CONTENT',
    '_pin_group',
    'actor',
    'forwarded_children',
    'forwarded_quote',
    'make_adapter',
    'runtime_target',
    'sdk_event',
    'sdk_event_with_content',
]
