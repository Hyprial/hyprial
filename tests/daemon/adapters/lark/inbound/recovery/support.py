"""Recovery paths for lost Lark inbound messages.

Covers the three layers added for the "websocket events are never replayed"
gap:

1. Failure-path preservation: bodies of inbound messages that fail to
   deliver (or fail to parse, or have no route) land in the state store's
   dead-letter trail — truncated, bounded, credential-free.
2. Native-id recovery: ``recover_message`` pulls the original body back from
   the server with ``im.v1.message.get`` and re-drives the inbound pipeline.
3. Reconnect reconciliation: ``reconcile_recent`` sweeps per-chat history
   after a websocket reconnect and re-drives only missed messages.

Idempotency (same native message id forwarded at most once, across live
events, manual recovery, and reconcile sweeps) is the hard requirement
under test throughout.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from lark_oapi import EventDispatcherHandler

from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    HarnessReceipt,
    LarkAdapter,
    LarkHistoryBatch,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.kernel import Logger

NOW_MS = 1_720_000_005_000
LOOKBACK_SECONDS = 900
WINDOW_START_MS = NOW_MS - LOOKBACK_SECONDS * 1000


def sdk_event(
    callback,
    *,
    event_id: str,
    message_id: str,
    chat_id: str = "oc_dm",
    chat_type: str = "p2p",
    text: str = "please investigate",
    content: str | None = None,
    message_type: str = "text",
    reply_to: str | None = None,
) -> None:
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
                "parent_id": reply_to,
                "create_time": "1720000001000",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content or json.dumps({"text": text}),
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


def inbound_message(
    message_id: str,
    *,
    chat_id: str = "oc_dm",
    chat_type: str = "unknown",
    text: str = "recovered body",
    create_time: str = "1720000001000",
    message_type: str = "text",
    content_status: str = "supported",
) -> LarkInboundMessage:
    """The shape ``get_inbound_message``/``list_chat_messages`` produce."""

    return LarkInboundMessage(
        event_id=f"recover:{message_id}",
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        text=text,
        sender_id="ou_sender",
        sender_type="user",
        create_time=create_time,
        message_type=message_type,
        content_status=content_status,
    )


class FakeLark:
    """LarkApiPort double; mirrors the SDK seam where ``chat_type`` comes
    from caller-supplied knowledge (the REST models carry none)."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.reactions: list[str] = []
        self.inbound: dict[str, LarkInboundMessage] = {}
        self.history: dict[str, tuple[LarkInboundMessage, ...]] = {}
        self.history_errors: dict[str, Exception] = {}
        self.get_calls: list[str] = []
        self.list_calls: list[tuple[str, str | None, str | None]] = []

    @staticmethod
    def _apply_chat_type(
        messages: tuple[LarkInboundMessage, ...], chat_type: str | None
    ) -> tuple[LarkInboundMessage, ...]:
        if chat_type is None:
            return messages
        applied = tuple(replace(item, chat_type=chat_type) for item in messages)
        if hasattr(messages, "complete"):
            return LarkHistoryBatch(
                applied,
                complete=bool(getattr(messages, "complete")),
            )
        return applied

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        self.reactions.append(f"{message_id}:{emoji_type}")

    def clear_reaction(self, message_id: str, emoji_type: str) -> None:
        pass

    def reply(
        self, message_id: str, text: str, *, idempotency_key: str
    ) -> str:
        del idempotency_key
        self.replies.append((message_id, text))
        return "om_guidance"

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        return "om_owner_dm"

    def get_message(self, message_id: str):  # type: ignore[no-untyped-def]
        return None

    def get_inbound_message(
        self, message_id: str, *, chat_type: str | None = None
    ) -> LarkInboundMessage | None:
        self.get_calls.append(message_id)
        message = self.inbound.get(message_id)
        if message is None:
            return None
        return self._apply_chat_type((message,), chat_type)[0]

    def list_chat_messages(
        self,
        chat_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = 50,
        max_pages: int = 20,
        chat_type: str | None = None,
    ) -> tuple[LarkInboundMessage, ...]:
        self.list_calls.append((chat_id, start_time, chat_type))
        if chat_id in self.history_errors:
            raise self.history_errors[chat_id]
        return self._apply_chat_type(self.history.get(chat_id, ()), chat_type)


class FakeHarness:
    def __init__(self) -> None:
        self.requests = []
        self.fail = False

    def send_request(self, request, *, timeout=None):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("daemon unreachable")
        self.requests.append(request)
        return HarnessReceipt(message_id=f"msg_harness_{len(self.requests)}")

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        pass

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        pass


class FakeRoutes:
    def __init__(self) -> None:
        self.pins: dict[str, ActorTarget] = {}
        self.mentions: dict[str, ActorTarget] = {}

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return next(
            (actor for actor in self.pins.values() if actor.actor_key == actor_key),
            None,
        )

    def resolve_mention(self, name: str) -> ActorTarget | None:
        return self.mentions.get(name)

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        return self.pins.get(conversation_id)


@pytest.fixture
def actor() -> ActorTarget:
    return ActorTarget(
        actor_id="agent:alice:codex:main",
        actor_key="alice/codex/main",
        display_name="alice/codex/main",
    )


def make_adapter(
    tmp_path: Path,
    actor: ActorTarget | None = None,
    *,
    logger: Logger | None = None,
    notify_operator=None,  # type: ignore[no-untyped-def]
    dead_letter_alert_threshold: int = 10,
    utcnow=None,  # type: ignore[no-untyped-def]
    inline_backfill_budget: float | None = None,
):
    lark = FakeLark()
    harness = FakeHarness()
    routes = FakeRoutes()
    if actor is not None:
        routes.pins["lark:oc_dm:oc_dm"] = actor
    budget: dict[str, object] = {}
    if inline_backfill_budget is not None:
        budget["inline_backfill_budget"] = inline_backfill_budget
    adapter = LarkAdapter(
        state=LarkStateStore(tmp_path / "lark-state.sqlite3"),
        lark=lark,
        harness=harness,
        routes=routes,
        channel_actor_id="channel:lark:test",
        logger=logger,
        notify_operator=notify_operator,
        dead_letter_alert_threshold=dead_letter_alert_threshold,
        utcnow=utcnow,
        **budget,
    )
    return adapter, lark, harness, routes


# ---------------------------------------------------------------------------
# Phase 1: failure paths preserve the body
# ---------------------------------------------------------------------------






























# ---------------------------------------------------------------------------
# Phase 2: recovery by native message id
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# Phase 3: reconnect reconciliation
# ---------------------------------------------------------------------------














































# ---------------------------------------------------------------------------
# Completing a954c161's criterion: a missing scope is not the only way for a
# chat to be beyond retry.
#
# That change split failures by *whether retrying could ever clear them*,
# because a fail-closed gate whose condition can never clear is a restart
# loop, not a gate.  It then recognised exactly one permanent kind -- a
# missing scope -- and left `errors` as the default bucket, so every other
# permanent failure kept its old behaviour: fail the probe, every sweep,
# forever.  A chat the bot was removed from is the case that actually occurs.
#
# These assert the QUARANTINE OUTCOME via _reconcile_health_result, not just
# which tuple the string landed in: "it went to blocked_chats" and "it no
# longer kills the adapter" are different claims, and only the second is the
# reason this change exists.
# ---------------------------------------------------------------------------


class RecordingLogger:
    """Captures structured events; asserts on fields, never on free text.

    ⛔ Its method surface must match the real ``hyprial.kernel.impl.log.Logger``: no
    catch-all ``__getattr__``, and no name the real object does not have.
    This stub once had both, and that is why a production defect survived:
    the adapter called ``self._logger.warning(...)``, which this stub
    answered while the real Logger raised ``AttributeError`` inside an
    ``except Exception: pass``, so the per-chat cause never reached a log.
    The real-Logger binding of that same path is
    ``test_the_unreadable_chat_emit_reaches_the_real_logger``; a missing
    method now raises here instead of recording nothing.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def bind(self, **_fields: object) -> "RecordingLogger":
        # The adapter stores ``logger.bind(...)``, so a stub whose bind()
        # returns None silently disables logging and every assertion here
        # passes vacuously against an empty list.
        return self

    def log(self, level: str, event: str, **fields: object) -> None:
        self.events.append((event, {"level": level, **fields}))

    def debug(self, event: str, **fields: object) -> None:
        self.events.append((event, {"level": "debug", **fields}))

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, {"level": "info", **fields}))

    def warn(self, event: str, **fields: object) -> None:
        self.events.append((event, {"level": "warn", **fields}))

    def error(self, event: str, **fields: object) -> None:
        self.events.append((event, {"level": "error", **fields}))


def _sweep_with_history_error(
    tmp_path: Path, actor: ActorTarget, error: Exception, *, logger=None
):  # type: ignore[no-untyped-def]
    adapter, lark, _harness, _routes = make_adapter(tmp_path, actor, logger=logger)
    sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
    lark.history_errors["oc_dm"] = error
    return adapter.reconcile_recent(
        lookback_seconds=LOOKBACK_SECONDS, now_ms=NOW_MS
    )










def _sweep_started_events(logger: Logger) -> list[dict[str, object]]:
    """Every ``adapter.reconcile.sweep_started`` the real logger recorded."""

    if not logger.path.exists():
        return []
    records = [json.loads(line) for line in logger.path.read_text().splitlines()]
    return [
        item
        for item in records
        if item.get("event") == "adapter.reconcile.sweep_started"
    ]


def _exploding_recent_chats() -> tuple[str, ...]:
    raise sqlite3.OperationalError("synthetic scan-set failure")


def _exploding_create_time(_value: object) -> int:
    raise TypeError("synthetic in-loop failure")






# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FakeHarness',
    'FakeLark',
    'FakeRoutes',
    'LOOKBACK_SECONDS',
    'NOW_MS',
    'RecordingLogger',
    'WINDOW_START_MS',
    '_exploding_create_time',
    '_exploding_recent_chats',
    '_sweep_started_events',
    '_sweep_with_history_error',
    'actor',
    'inbound_message',
    'make_adapter',
    'sdk_event',
]
