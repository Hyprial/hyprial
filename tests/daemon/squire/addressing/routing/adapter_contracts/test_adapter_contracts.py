from __future__ import annotations

import json
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.identity.impl.users.profile import UserProfileStore
from hyprial.daemon.impl.squire.addressing import UNAVAILABLE_ADAPTER_MESSAGE, ReceiverUserDelivery, UserAdapterRegistry, UserDeliveryLedger, UserDeliveryResult
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from ..support import (
    FakeOwnerAdapter,
    FakeUserDelivery,
    _PendingFakeInbox,
    _TEST_APPLICATIONS,
    _application,
    _close_test_applications as _close_test_applications,
    _request,
)


def test_daemon_registers_receiver_squire_adapter_from_persistent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "receiver-home"
    state = home / "state"
    adapter_uri = "channel:bob:receiver-mac:squire-app"
    (home / "secrets").mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "squire-app",
                        "appId": "cli_receiver_squire",
                        "credentialRef": "lark-squire-app",
                        "routes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (home / "secrets" / "lark-squire-app.json").write_text(
        json.dumps({"appSecret": "isolated-receiver-secret"}),
        encoding="utf-8",
    )
    application = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="receiver-mac",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    _TEST_APPLICATIONS.append(application)
    application.user_profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    application.user_profiles.set_squire_channel("owner-bob", adapter_uri)
    calls: list[tuple[str, str]] = []
    adapter = FakeOwnerAdapter()

    def from_credentials(app_id: str, app_secret: str) -> FakeOwnerAdapter:
        calls.append((app_id, app_secret))
        return adapter

    monkeypatch.setattr(LarkSdkGateway, "from_credentials", from_credentials)

    configured = application.configure_user_adapters(
        application.load_persistent_configuration()
    )

    assert configured == (adapter_uri,)
    owner = application.user_adapters.get(adapter_uri)
    assert isinstance(owner, GatewayIoAuthority)
    assert owner.send_owner_dm("ou_test", "hello", idempotency_key="test-send") == "om-user-delivery"
    assert adapter.calls == [("ou_test", "hello", "test-send")]
    assert calls == [("cli_receiver_squire", "isolated-receiver-secret")]


def test_daemon_rejects_channel_route_target_instead_of_silently_queuing(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": "agent:alice:sender-mac:worker",
                "to": ["channel:alice:mac:feishu-main:hyprial-all-member"],
                "summary": "kanban broadcast",
                "message": "kanban broadcast",
                "idempotencyKey": "operation-1",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert "user:<owner>" in str(failure.value)
    # the message must never reach the agent inbox, where it would queue
    # forever behind ok:true
    assert inbox.submitted == []
    assert delivery.requests == []


def test_daemon_rejects_four_part_channel_target_as_well(tmp_path: Path) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": "agent:alice:sender-mac:worker",
                "to": ["channel:alice:mac:feishu-main"],
                "summary": "hello",
                "message": "hello",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert inbox.submitted == []


def test_daemon_rejects_four_part_adapter_target_the_same_as_channel(
    tmp_path: Path,
) -> None:
    """classify_target_identity has no per-scheme special case for
    channel:/adapter: -- both fall through to the generic unknown verdict,
    so the send gate rejects the current adapter: spelling identically to
    the legacy channel: spelling with zero code change required here; this
    test pins that fact so it stays true across future refactors."""

    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, inbox = _application(tmp_path, delivery)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.send",
            {
                "from": "agent:alice:sender-mac:worker",
                "to": ["adapter:alice:mac:feishu-main"],
                "summary": "hello",
                "message": "hello",
            },
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert inbox.submitted == []


def test_pi_harness_reply_to_lark_inbound_enters_the_reply_bridge(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-lark-inbound",
        conversation_id="lark:oc-group:oc-group",
        sender="channel:lark:mac-studio-feishu",
        recipient="agent:alice:sender-mac:worker",
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    result = app.handle(
        "message.reply",
        {
            "from": "agent:alice:sender-mac:worker",
            "messageId": "msg-lark-inbound",
            "summary": "pong",
            "message": "pong",
        },
    )

    assert result["ok"] is True
    assert result["replied"] is True
    assert result["acknowledged"] is True
    assert len(inbox.submitted) == 1
    reply = inbox.submitted[0]
    assert reply.recipient == "channel:lark:mac-studio-feishu"
    assert reply.intent == "reply"
    assert reply.idempotency_key == "reply:msg-lark-inbound"
    assert json.loads(reply.payload) == {
        "message": "pong",
        "origin": {"via": "daemon"},
        "replyTo": "msg-lark-inbound",
    }


def test_harness_reply_fails_loudly_for_unconfigured_lark_adapter(
    tmp_path: Path,
) -> None:
    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-lark-inbound",
        conversation_id="lark:oc-group:oc-group",
        sender="channel:lark:missing-adapter",
        recipient="agent:alice:sender-mac:worker",
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    class Adapters:
        gateway_names: tuple[str, ...] = ("configured-adapter",)

    app._adapters = Adapters()  # type: ignore[assignment]

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": "agent:alice:sender-mac:worker",
                "messageId": "msg-lark-inbound",
                "summary": "pong",
                "message": "pong",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []


def test_harness_reply_rejects_bare_four_part_adapter_actor_without_reply_bridge(
    tmp_path: Path,
) -> None:
    """The four-segment adapter:<owner>:<machine>:<name> form (not the
    three-segment adapter:lark:<adapter> reply-bridge form) must be rejected
    exactly like its legacy channel:<owner>:<machine>:<name> counterpart —
    this exercises the application.py branch that dual-reads
    CHANNEL_URI_PREFIX/ADAPTER_URI_PREFIX for the "no reply bridge" gate."""

    delivery = FakeUserDelivery(
        UserDeliveryResult(message_id="msg-user-1", accepted=True)
    )
    app, _inbox = _application(tmp_path, delivery)
    original = InboxMessage(
        message_id="msg-bare-adapter",
        conversation_id="lark:oc-group:oc-group",
        sender="adapter:alice:mac:feishu-main",
        recipient="agent:alice:sender-mac:worker",
        payload=b'{"message":"ping"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox = _PendingFakeInbox(original)
    app._inbox = inbox

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": "agent:alice:sender-mac:worker",
                "messageId": "msg-bare-adapter",
                "summary": "pong",
                "message": "pong",
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_UNAVAILABLE"
    assert inbox.submitted == []


def test_receiver_reloads_adapter_registry_after_late_squire_setup(
    tmp_path: Path,
) -> None:
    """`hyprial squire setup` completed after the daemon started must not require
    a daemon restart before user delivery can succeed."""

    profiles = UserProfileStore(tmp_path / "receiver-home" / "state" / "users.json")
    profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    adapter_uri = "channel:bob:receiver-mac:squire-app"
    profiles.set_squire_channel("owner-bob", adapter_uri)
    profiles.bind_owner_open_id(
        "owner-bob", channel=adapter_uri, open_id="ou_receiver_owner"
    )
    adapter = FakeOwnerAdapter()
    adapters = UserAdapterRegistry()  # daemon started before setup completed
    reloads: list[str] = []

    def reload_adapters() -> None:
        reloads.append("called")
        adapters.register(adapter_uri, adapter)

    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        profiles=profiles,
        adapters=adapters,
        ledger=UserDeliveryLedger(
            tmp_path / "receiver-home" / "state" / "user-deliveries.json"
        ),
        reload_adapters=reload_adapters,
    )

    result = receiver.handle(_request())

    assert reloads == ["called"]
    assert result.accepted is True
    assert adapter.calls[0][0] == "ou_receiver_owner"


def test_receiver_reload_still_fails_loudly_when_adapter_stays_unconfigured(
    tmp_path: Path,
) -> None:
    profiles = UserProfileStore(tmp_path / "receiver-home" / "state" / "users.json")
    profiles.ensure(
        owner="bob",
        owner_key="owner-bob",
        login_name="bob",
        machine="receiver-mac",
        machine_key="receiver-machine-key",
    )
    adapter_uri = "channel:bob:receiver-mac:squire-app"
    profiles.set_squire_channel("owner-bob", adapter_uri)
    profiles.bind_owner_open_id(
        "owner-bob", channel=adapter_uri, open_id="ou_receiver_owner"
    )
    reloads: list[str] = []
    receiver = ReceiverUserDelivery(
        node_id="receiver-mac",
        profiles=profiles,
        adapters=UserAdapterRegistry(),
        ledger=UserDeliveryLedger(
            tmp_path / "receiver-home" / "state" / "user-deliveries.json"
        ),
        reload_adapters=lambda: reloads.append("called"),
    )

    result = receiver.handle(_request())

    assert reloads == ["called"]
    assert result.accepted is False
    assert result.code == "TARGET_SQUIRE_ADAPTER_UNAVAILABLE"
    assert result.message == UNAVAILABLE_ADAPTER_MESSAGE
