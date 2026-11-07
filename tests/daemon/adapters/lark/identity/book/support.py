"""Identity mapping: platform ids ↔ display names ↔ hyprial owners, on record.

Covers the incident-driven guarantees: the schema upgrade that adds the
``identities`` table to existing v2 databases, idempotent observation that
never downgrades a human-verified mapping, per-adapter namespacing (open_ids
are scoped per App), the sync collector draining *every* pagination cursor
(a truncated member view once produced a confidently wrong who-is-who), and
the CLI surface.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark import (
    LarkAdapter,
    LarkBotInfo,
    LarkChatMember,
    LarkChatSummary,
    LarkInboundMessage,
    LarkStateStore,
)
def store_at(tmp_path: Path, adapter: str = "lark:main") -> LarkStateStore:
    return LarkStateStore(tmp_path / "adapters.sqlite3", adapter=adapter)


# ---------------------------------------------------------------------------
# Schema migration




















# ---------------------------------------------------------------------------
# Observation and verification semantics














# ---------------------------------------------------------------------------
# Active collection (sync) — pagination is the point


class FakeCollectionGateway:
    """Page-serving fake; every listing spans two pages."""

    def __init__(self) -> None:
        self.chat_page_tokens: list[str | None] = []
        self.member_page_tokens: list[tuple[str, str | None]] = []
        self.fail_members_for: set[str] = set()
        self.endless_member_chats: set[str] = set()

    def bot_info(self) -> LarkBotInfo:
        return LarkBotInfo(open_id="ou_bot_self", app_name="HQ Adjutant")

    def list_chats_page(
        self, *, page_token: str | None = None, page_size: int = 50
    ):
        self.chat_page_tokens.append(page_token)
        if page_token is None:
            return (LarkChatSummary("oc_all", name="全员群"),), "chats-page-2"
        assert page_token == "chats-page-2"
        return (LarkChatSummary("oc_dev", name="dev"),), None

    def list_chat_members_page(
        self,
        chat_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 50,
    ):
        self.member_page_tokens.append((chat_id, page_token))
        if chat_id in self.fail_members_for:
            raise RuntimeError("boom https://secret.example/?token=hush")
        if chat_id in self.endless_member_chats:
            return (LarkChatMember("ou_loop", name="loop"),), "again", 99
        if page_token is None:
            first = tuple(
                LarkChatMember(f"ou_{chat_id}_{index}", name=f"成员{index}")
                for index in range(3)
            )
            return first, "members-page-2", 5
        assert page_token == "members-page-2"
        rest = tuple(
            LarkChatMember(f"ou_{chat_id}_{index}", name=f"成员{index}")
            for index in range(3, 5)
        )
        return rest, None, 5










# ---------------------------------------------------------------------------
# Passive collection on the inbound path


def _minimal_adapter(tmp_path: Path) -> tuple[LarkAdapter, LarkStateStore]:
    state = LarkStateStore(tmp_path / "adapters.sqlite3", adapter="lark:main")
    lark = SimpleNamespace(
        bot_open_id=lambda: "ou_bot_self",
        add_reaction=lambda message_id, emoji: None,
        reply=lambda message_id, text, *, idempotency_key: "om_reply",
        get_message=lambda message_id: None,
    )
    routes = SimpleNamespace(
        actor_by_key=lambda key: None,
        pinned_actor=lambda conversation_id: None,
        list_runtime_targets=lambda: (),
        resolve_runtime_target=lambda token: (),
    )
    adapter = LarkAdapter(
        state=state,
        lark=lark,  # type: ignore[arg-type]
        harness=SimpleNamespace(  # type: ignore[arg-type]
            send_request=lambda request: SimpleNamespace(message_id="hm_1"),
            accept_delivery=lambda *args, **kwargs: None,
            reject_delivery=lambda *args, **kwargs: None,
        ),
        routes=routes,  # type: ignore[arg-type]
        channel_actor_id="channel:lark:main",
    )
    return adapter, state


def inbound(message_id: str, **overrides: object) -> LarkInboundMessage:
    values: dict[str, object] = {
        "event_id": f"evt-{message_id}",
        "message_id": message_id,
        "chat_id": "oc_group",
        "chat_type": "group",
        "text": "hello",
        "sender_id": "ou_sender",
        "sender_type": "user",
        "sender_union_id": "on_sender",
    }
    values.update(overrides)
    return LarkInboundMessage(**values)  # type: ignore[arg-type]












# ---------------------------------------------------------------------------
# SDK boundary


def _ok(data: object) -> SimpleNamespace:
    return SimpleNamespace(success=lambda: True, code=0, msg="ok", data=data)








# ---------------------------------------------------------------------------
# CLI surface


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hyprial-home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    return state_dir










# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FakeCollectionGateway',
    '_cli_env',
    '_minimal_adapter',
    '_ok',
    'inbound',
    'store_at',
]
