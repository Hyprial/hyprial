from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from hyprial.daemon.impl.adapters.lark import LarkBotInfo, LarkChatMember, LarkChatSummary, LarkInboundMessage, sync_identities

from ..support import (
    FakeCollectionGateway,
    _minimal_adapter,
    _ok,
    store_at,
)


def test_sync_drains_every_page_of_chats_and_members(tmp_path: Path) -> None:
    """The regression for the head-8 incident: page 2 must be fetched."""

    store = store_at(tmp_path)
    gateway = FakeCollectionGateway()

    report = sync_identities(
        state=store, gateway=gateway, app_id="cli_app", now_ms=1_000
    )

    # Both chat pages and both member pages per chat were requested.
    assert gateway.chat_page_tokens == [None, "chats-page-2"]
    assert gateway.member_page_tokens == [
        ("oc_all", None),
        ("oc_all", "members-page-2"),
        ("oc_dev", None),
        ("oc_dev", "members-page-2"),
    ]
    # Every member of every page landed in the store, with names.
    users = store.identities(kind="user")
    assert len(users) == 10
    assert {row.platform_id for row in users} == {
        f"ou_{chat}_{index}"
        for chat in ("oc_all", "oc_dev")
        for index in range(5)
    }
    assert all(row.display_name for row in users)
    assert all(row.standing == "observed" for row in users)
    assert {row.source for row in users} == {
        "chat-members:oc_all",
        "chat-members:oc_dev",
    }
    # The app and its bot are recorded in their own kinds.
    app = store.identity("app", "cli_app")
    bot = store.identity("bot", "ou_bot_self")
    assert app is not None and app.display_name == "HQ Adjutant"
    assert bot is not None and bot.display_name == "HQ Adjutant"
    # The report proves completeness against the platform's own totals.
    assert report["ok"] is True
    assert report["chatsComplete"] is True
    assert report["identitiesRecorded"] == 12
    assert [
        (chat["chatId"], chat["memberTotal"], chat["membersRecorded"])
        for chat in report["chats"]
    ] == [("oc_all", 5, 5), ("oc_dev", 5, 5)]
    assert all(chat["complete"] for chat in report["chats"])


def test_sync_is_idempotent_across_runs(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    gateway = FakeCollectionGateway()
    sync_identities(state=store, gateway=gateway, app_id="cli_app", now_ms=1_000)
    first = store.identities()

    sync_identities(state=store, gateway=gateway, app_id="cli_app", now_ms=2_000)

    second = store.identities()
    assert len(second) == len(first) == 12
    assert all(row.first_seen_ms == 1_000 for row in second)
    assert all(row.last_seen_ms == 2_000 for row in second)


def test_sync_isolates_per_chat_failures_and_reports_them(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    gateway = FakeCollectionGateway()
    gateway.fail_members_for.add("oc_all")

    report = sync_identities(
        state=store, gateway=gateway, app_id="cli_app", now_ms=1_000
    )

    # The other chat was still collected in full.
    assert len(store.identities(kind="user")) == 5
    assert report["ok"] is False
    assert report["errors"] == ["oc_all: members-unavailable (RuntimeError)"]
    # Platform error text (which can carry URLs/tokens) stays out of reports.
    assert "hush" not in json.dumps(report)


def test_sync_reports_an_unfinishable_pagination_instead_of_lying(
    tmp_path: Path,
) -> None:
    """A cursor that never ends must surface as incomplete, never as done."""

    store = store_at(tmp_path)
    gateway = FakeCollectionGateway()
    gateway.endless_member_chats.add("oc_dev")

    report = sync_identities(
        state=store, gateway=gateway, app_id="cli_app", now_ms=1_000, max_pages=7
    )

    dev = next(chat for chat in report["chats"] if chat["chatId"] == "oc_dev")
    assert dev["complete"] is False
    assert report["ok"] is False
    assert any("oc_dev: members-incomplete" in error for error in report["errors"])
    # Bounded: exactly max_pages requests, not an infinite crawl.
    dev_requests = [
        token for chat, token in gateway.member_page_tokens if chat == "oc_dev"
    ]
    assert len(dev_requests) == 7


def test_member_change_events_record_named_identities(tmp_path: Path) -> None:
    adapter, state = _minimal_adapter(tmp_path)
    event = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="oc_group",
            users=[
                SimpleNamespace(
                    name="林懿伦",
                    user_id=SimpleNamespace(
                        open_id="ou_lin", union_id="on_lin"
                    ),
                ),
                SimpleNamespace(name=None, user_id=SimpleNamespace(open_id="")),
            ],
        )
    )

    adapter.handle_member_change_event(event)

    row = state.identity("user", "ou_lin")
    assert row is not None
    assert row.display_name == "林懿伦"
    assert row.union_id == "on_lin"
    assert row.source == "member-event:oc_group"
    assert len(state.identities()) == 1  # the empty open_id was skipped


def test_sdk_events_carry_the_sender_union_id(tmp_path: Path) -> None:
    """The live event payload's union_id must survive normalization."""

    from lark_oapi import EventDispatcherHandler

    from hyprial.daemon.impl.adapters.lark import normalize_sdk_event

    captured: list[LarkInboundMessage] = []
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_u",
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
                "message_id": "om_u",
                "create_time": "1720000000000",
                "chat_id": "oc_dm",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
                "mentions": [],
            },
        },
    }
    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            lambda event: captured.append(normalize_sdk_event(event))
        )
        .build()
    )
    dispatcher._do_without_validation(json.dumps(payload).encode())

    assert captured[0].sender_union_id == "on_sender"
    assert captured[0].sender_id == "ou_sender"


def test_sdk_gateway_pages_chats_and_members_by_open_id() -> None:
    from hyprial.daemon.impl.adapters.lark import LarkSdkGateway

    chat_requests: list[object] = []
    member_requests: list[object] = []

    def chat_list(request: object) -> SimpleNamespace:
        chat_requests.append(request)
        return _ok(
            SimpleNamespace(
                items=[
                    SimpleNamespace(chat_id="oc_all", name="全员群"),
                    SimpleNamespace(chat_id="", name="dropped"),
                ],
                has_more=True,
                page_token="next-chats",
            )
        )

    def members_get(request: object) -> SimpleNamespace:
        member_requests.append(request)
        return _ok(
            SimpleNamespace(
                items=[
                    SimpleNamespace(member_id="ou_a", name="甲"),
                    SimpleNamespace(member_id=None, name="dropped"),
                ],
                has_more=False,
                page_token="ignored-when-not-has-more",
                member_total=7,
            )
        )

    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                chat=SimpleNamespace(list=chat_list),
                chat_members=SimpleNamespace(get=members_get),
            )
        )
    )
    gateway = LarkSdkGateway(client)

    chats, chats_next = gateway.list_chats_page(page_token="page-2")
    members, members_next, total = gateway.list_chat_members_page(
        "oc_all", page_token="m-page-2"
    )

    assert chats == (LarkChatSummary("oc_all", name="全员群"),)
    assert chats_next == "next-chats"
    assert chat_requests[0].page_token == "page-2"
    assert members == (LarkChatMember("ou_a", name="甲"),)
    assert members_next is None  # has_more=False ends the cursor
    assert total == 7
    assert member_requests[0].chat_id == "oc_all"
    assert member_requests[0].member_id_type == "open_id"
    assert member_requests[0].page_token == "m-page-2"


def test_sdk_gateway_bot_info_is_loud_on_failure() -> None:
    from hyprial.daemon.impl.adapters.lark import LarkSdkError, LarkSdkGateway

    def request_ok(request: object) -> SimpleNamespace:
        return SimpleNamespace(
            raw=SimpleNamespace(
                content=json.dumps(
                    {
                        "code": 0,
                        "bot": {
                            "open_id": "ou_bot_self",
                            "app_name": "HQ Adjutant",
                        },
                    }
                ).encode("utf-8")
            )
        )

    info = LarkSdkGateway(SimpleNamespace(request=request_ok)).bot_info()
    assert info == LarkBotInfo(open_id="ou_bot_self", app_name="HQ Adjutant")

    def request_denied(request: object) -> SimpleNamespace:
        return SimpleNamespace(
            raw=SimpleNamespace(
                content=json.dumps({"code": 99991663, "msg": "no"}).encode()
            )
        )

    with pytest.raises(LarkSdkError, match="bot info failed"):
        LarkSdkGateway(SimpleNamespace(request=request_denied)).bot_info()


def test_event_stream_registers_member_change_events_when_asked() -> None:
    from hyprial.daemon.impl.adapters.lark import LarkEventStream

    captured: dict[str, object] = {}
    seen: list[str] = []

    def factory(app_id: str, app_secret: str, dispatcher: object) -> object:
        captured["dispatcher"] = dispatcher
        return SimpleNamespace(start=lambda: None)

    LarkEventStream(
        app_id="cli_app",
        app_secret="secret",
        on_message=lambda event: seen.append("message"),
        on_member_change=lambda event: seen.append(
            f"member:{event.event.chat_id}:"
            + ",".join(user.name for user in event.event.users)
        ),
        client_factory=factory,
    )

    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_member",
            "event_type": "im.chat.member.user.added_v1",
            "create_time": "1720000000000",
            "token": "token",
            "app_id": "cli_app",
            "tenant_key": "tenant",
        },
        "event": {
            "chat_id": "oc_all",
            "users": [
                {
                    "name": "林懿伦",
                    "user_id": {"open_id": "ou_lin", "union_id": "on_lin"},
                }
            ],
        },
    }
    captured["dispatcher"]._do_without_validation(json.dumps(payload).encode())

    assert seen == ["member:oc_all:林懿伦"]
