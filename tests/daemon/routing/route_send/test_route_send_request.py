"""I4b: ``adapter.routes.send`` posts to a route named by the adapter's own route name.

Decision notes (I4b, owner item Q0-d, 2026-11-23): the Lark reauth notice
names a route by its bare name; the daemon resolves the one configured
adapter that has it and posts through the I3 primitive, with the
``转述自 <sender>`` attribution.  No new error code: an unknown name is
``ROUTE_NOT_FOUND`` and a name on several adapters is ``INVALID_ARGUMENT``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.conversations.bypass.bypass_support import boot
from tests.support.golden.app import ADAPTER, ALPHA, golden_daemon as golden_daemon
from tests.support.identity.cell_push import fail_chats

GROUP = "oc_group_chat"


def _send(app: Any, route: str) -> Any:
    return app.handle(
        "adapter.routes.send", {"route": route, "message": "hello", "sender": ALPHA},
    )


def test_a_route_name_posts_to_its_one_adapter_with_attribution(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    gateway = booted.daemon.gateway
    before = len(gateway.calls)

    result = _send(booted.daemon.app, "group")

    [(chat, text, _)] = gateway.calls[before:]
    assert (chat, text) == (GROUP, f"转述自 {ALPHA}：\n\nhello")
    assert result["ok"] is True
    assert (result["adapter"], result["route"]) == (ADAPTER, "group")
    assert [post["chatId"] for post in result["posts"]] == [GROUP]


def test_an_unknown_route_name_is_route_not_found_and_posts_nothing(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    gateway = booted.daemon.gateway
    before = len(gateway.calls)

    with pytest.raises(DaemonRequestError) as raised:
        _send(booted.daemon.app, "nowhere")

    assert raised.value.code == "ROUTE_NOT_FOUND"
    assert gateway.calls[before:] == []


def test_a_route_name_on_two_adapters_is_refused(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.daemon.app
    path = app.hyprial_home / "channels.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["gateways"].append({
        "provider": "lark", "name": "second-lark", "appId": "cli_second",
        "credentialRef": "lark-second-lark",
        "routes": [{"name": "group", "type": "direct", "nativeId": "oc_other"}],
    })
    path.write_text(json.dumps(config), encoding="utf-8")
    (app.hyprial_home / "secrets" / "lark-second-lark.json").write_text(
        json.dumps({"appSecret": "isolated-second-secret"}), encoding="utf-8",
    )
    gateway = booted.daemon.gateway
    before = len(gateway.calls)

    with pytest.raises(DaemonRequestError) as raised:
        _send(app, "group")

    assert raised.value.code == ipc_errors.INVALID_ARGUMENT
    assert gateway.calls[before:] == []


def test_an_unregistered_sender_is_refused_before_anything_is_posted(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    gateway = booted.daemon.gateway
    before = len(gateway.calls)

    with pytest.raises(DaemonRequestError) as raised:
        booted.daemon.app.handle(
            "adapter.routes.send", {"route": "group", "message": "hello", "sender": "nobody-here"},
        )

    assert raised.value.code == ipc_errors.SENDER_UNRESOLVED
    assert gateway.calls[before:] == []


def test_a_platform_refusal_is_user_delivery_failed(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)
    fail_chats(booted.daemon.gateway, monkeypatch, {
        GROUP: LarkApiError("send message", 230002, "bot not in chat"),
    })

    with pytest.raises(DaemonRequestError) as raised:
        _send(booted.daemon.app, "group")

    assert raised.value.code == ipc_errors.USER_DELIVERY_FAILED


@pytest.mark.parametrize(
    "sender", ["agent:op:node-1:ghost", "agent:other:elsewhere:ghost"], ids=["this-node", "another-node"],
)
def test_an_unregistered_full_agent_uri_sender_is_refused_before_anything_is_posted(
    golden_daemon, monkeypatch, sender
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    gateway = booted.daemon.gateway
    before = len(gateway.calls)

    with pytest.raises(DaemonRequestError) as raised:
        booted.daemon.app.handle(
            "adapter.routes.send", {"route": "group", "message": "hello", "sender": sender},
        )

    assert raised.value.code == ipc_errors.SENDER_UNRESOLVED
    assert raised.value.data == {"sender": sender}
    assert gateway.calls[before:] == []


def test_the_post_and_its_refusal_are_logged_with_the_adapter(golden_daemon, monkeypatch) -> None:
    booted = boot(golden_daemon, monkeypatch)

    _send(booted.daemon.app, "group")
    fail_chats(booted.daemon.gateway, monkeypatch, {
        GROUP: LarkApiError("send message", 230002, "bot not in chat"),
    })
    with pytest.raises(DaemonRequestError):
        _send(booted.daemon.app, "group")

    sent, refused = booted.logs.named("adapter.routes.send")
    assert (sent["adapter"], sent["route"], sent["sender"], sent["state"]) == (ADAPTER, "group", ALPHA, "sent")
    assert (refused["adapter"], refused["route"], refused["state"]) == (ADAPTER, "group", "not-sent")
    assert (refused["reason"], refused["code"], refused["larkCode"]) == (
        "provider-refused", ipc_errors.USER_DELIVERY_FAILED, 230002,
    )
