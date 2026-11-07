from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
import lark_oapi as lark
from hyprial.daemon.impl.adapters.lark import LarkEventStream, LarkSdkGateway, normalize_sdk_event

from ..support import (
    FIXTURES,
    FakeClient,
    FakeWsClient,
    MERGE_FORWARD_SHELL_BODY,
    _BotInfoClient,
    install_get_items,
    response,
    rest_message,
)


def test_message_type_is_canonical_before_returning_from_rest() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)
    raw_type = "future_secret_type\nAuthorization: Bearer must-not-leak"
    client.message.get = lambda request: response(  # type: ignore[method-assign]
        data=SimpleNamespace(
            items=[
                rest_message(
                    "om_malicious_type",
                    msg_type=raw_type,
                    content={"access_token": "also-must-not-leak"},
                )
            ]
        )
    )

    message = gateway.get_inbound_message("om_malicious_type", chat_type="p2p")

    assert message is not None
    assert message.message_type == "unknown"
    assert message.content_status == "unsupported"
    assert message.text == "[Unsupported Lark message type]"
    assert "must-not-leak" not in repr(message)


def test_sdk_gateway_builds_generated_im_requests() -> None:
    client = FakeClient()
    gateway = LarkSdkGateway(client)

    gateway.add_reaction("om_in", "OnIt")
    owner_dm_id = gateway.send_owner_dm(
        "ou_owner", "relayed", idempotency_key="operation-user-1:bob"
    )
    reply_id = gateway.reply(
        "om_in", "hello", idempotency_key="om_in:command:help"
    )
    quote = gateway.get_message("om_quote")
    gateway.clear_reaction("om_in", "OnIt")

    assert reply_id == "om_reply"
    assert owner_dm_id == "om_owner_dm"
    assert quote is not None and quote.text == "quoted"
    create = client.reaction.creates[0]
    assert create.message_id == "om_in"
    assert create.request_body.reaction_type.emoji_type == "OnIt"
    reply = client.message.replies[0]
    assert reply.message_id == "om_in"
    assert json.loads(reply.request_body.content) == {"text": "hello"}
    assert reply.request_body.uuid == str(
        uuid5(NAMESPACE_URL, "hyprial:lark-reply:om_in:command:help")
    )
    owner_dm = client.message.creates[0]
    assert owner_dm.receive_id_type == "open_id"
    assert owner_dm.request_body.receive_id == "ou_owner"
    assert owner_dm.request_body.uuid == str(
        uuid5(NAMESPACE_URL, "hyprial:lark-owner-dm:operation-user-1:bob")
    )
    assert json.loads(owner_dm.request_body.content) == {"text": "relayed"}
    assert client.reaction.lists[0].reaction_type == "OnIt"
    assert [item.reaction_id for item in client.reaction.deletes] == ["reaction_app"]


def test_bot_open_id_is_fetched_once_and_cached() -> None:
    client = _BotInfoClient([{"code": 0, "bot": {"open_id": "ou_bot_self"}}])
    gateway = LarkSdkGateway(client)

    assert gateway.bot_open_id() == "ou_bot_self"
    assert gateway.bot_open_id() == "ou_bot_self"
    # One network round trip: the identity is immutable per app process.
    assert len(client.requests) == 1
    uri = getattr(client.requests[0], "uri", None)
    assert uri == "/open-apis/bot/v3/info"


def test_bot_open_id_failure_answers_none_and_retries_later() -> None:
    """No identity, no guessing -- and no permanently cached failure."""

    client = _BotInfoClient(
        [
            RuntimeError("network down"),
            {"code": 99991663, "msg": "forbidden"},
            {"code": 0, "bot": {"open_id": "ou_bot_self"}},
        ]
    )
    gateway = LarkSdkGateway(client)

    assert gateway.bot_open_id() is None  # transport failure
    assert gateway.bot_open_id() is None  # platform error payload
    assert gateway.bot_open_id() == "ou_bot_self"  # recovered
    assert len(client.requests) == 3


def test_event_stream_injects_dispatcher_and_starts_sdk_client() -> None:
    captured = {}
    client = FakeWsClient()

    def factory(app_id, app_secret, dispatcher):  # type: ignore[no-untyped-def]
        captured.update(
            app_id=app_id,
            app_secret=app_secret,
            dispatcher=dispatcher,
        )
        return client

    stream = LarkEventStream(
        app_id="cli_test_app",
        app_secret="synthetic_secret",
        on_message=lambda _event: None,
        client_factory=factory,
    )
    stream.start()

    assert captured["app_id"] == "cli_test_app"
    assert captured["app_secret"] == "synthetic_secret"
    assert captured["dispatcher"] is not None
    assert client.starts == 1

    stream.probe_rest_endpoint()
    assert client.probes == 1


def test_event_stream_observes_business_and_transport_activity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    observed: list[str] = []

    async def fake_handle_message(self, _message):  # type: ignore[no-untyped-def]
        observed.append("base-transport-handler")

    monkeypatch.setattr(lark.ws.Client, "_handle_message", fake_handle_message)
    async def drive() -> None:
        # The SDK eagerly creates its ExpiringCache task on the current loop.
        # Constructing the default client inside asyncio.run lets that runner
        # cancel and await the task during teardown, just as Client.start()
        # keeps the owning loop alive in the worker process.
        stream = LarkEventStream(
            app_id="cli_test_app",
            app_secret="synthetic_secret",
            on_message=lambda _event: observed.append("event"),
            on_transport_activity=lambda: observed.append("transport"),
            on_event_activity=lambda: observed.append("event-activity"),
        )
        await stream._client._handle_message(b"synthetic-control-or-data-frame")

        payload = (FIXTURES / "lark-p2-group-message.json").read_bytes()
        stream._client._event_handler._do_without_validation(payload)

    asyncio.run(drive())
    assert observed[:2] == ["transport", "base-transport-handler"]
    assert observed[-2:] == ["event-activity", "event"]


def test_default_event_stream_async_test_lifecycle_awaits_cache_cron() -> None:
    script = """
import asyncio
import gc

from hyprial.daemon.impl.adapters.lark import LarkEventStream


async def main() -> None:
    LarkEventStream(
        app_id="cli_test_app",
        app_secret="synthetic_secret",
        on_message=lambda _event: None,
    )


asyncio.run(main())
gc.collect()
"""
    completed = subprocess.run(
        [sys.executable, "-W", "default::RuntimeWarning", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ExpiringCache._start_clear_cron" not in completed.stderr
    assert "coroutine was never awaited" not in completed.stderr


def test_event_stream_dispatches_ts_observed_p2_group_message_shape() -> None:
    payload = (FIXTURES / "lark-p2-group-message.json").read_bytes()
    captured = {}
    normalized = []

    def factory(_app_id, _app_secret, dispatcher):  # type: ignore[no-untyped-def]
        captured["dispatcher"] = dispatcher
        return FakeWsClient()

    LarkEventStream(
        app_id="cli_offline_golden",
        app_secret="offline-secret",
        on_message=lambda event: normalized.append(normalize_sdk_event(event)),
        client_factory=factory,
    )

    captured["dispatcher"]._do_without_validation(payload)

    assert len(normalized) == 1
    message = normalized[0]
    assert message.event_id == "evt_offline_golden"
    assert message.message_id == "om_offline_golden"
    assert message.chat_id == "oc_offline_golden"
    assert message.chat_type == "group"
    assert message.message_type == "text"
    assert message.text == "OFFLINE_GOLDEN"
    assert message.mentions == ()


def test_orphaned_child_renders_at_top_level_instead_of_vanishing() -> None:
    """A child whose ``upper_message_id`` names a message absent from the
    response must still be shown, not withheld with a misleading limit note."""

    shell = "om_orphan_shell"
    items = [
        rest_message(
            shell, msg_type="merge_forward", raw_content=MERGE_FORWARD_SHELL_BODY
        ),
        rest_message(
            "om_orphan",
            upper_message_id="om_never_returned",
            text="orphaned but visible",
        ),
    ]
    client = FakeClient()
    install_get_items(client, items)
    gateway = LarkSdkGateway(client)

    quote = gateway.get_message(shell)

    assert quote is not None
    assert "orphaned but visible" in quote.text
    assert "not shown" not in quote.text


def test_ready_client_signals_ready_once_then_reconnect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    signals: list[str] = []

    async def fake_base_connect(self):  # type: ignore[no-untyped-def]
        self._conn = object()

    monkeypatch.setattr(lark.ws.Client, "_connect", fake_base_connect)

    async def drive() -> None:
        stream = LarkEventStream(
            app_id="cli_test_app",
            app_secret="synthetic_secret",
            on_message=lambda _event: None,
            on_ready=lambda: signals.append("ready"),
            on_reconnect=lambda: signals.append("reconnect"),
        )
        client = stream._client
        await client._connect()
        # Simulate two drop/reconnect cycles: the SDK clears _conn on
        # disconnect, so each reconnect runs _connect with _conn None.
        client._conn = None
        await client._connect()
        client._conn = None
        await client._connect()

    asyncio.run(drive())

    assert signals == ["ready", "reconnect", "reconnect"]
