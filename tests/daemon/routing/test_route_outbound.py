"""Lark send-error mapping, fanout resolution and the remaining non-route send guards (I4b)."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.network.route_delivery import (
    RouteDeliveryError,
    map_lark_send_error,
    resolve_gateway_routes,
)
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.kernel import ChannelConfiguration
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class FakeChatGateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.file_calls: list[tuple[str, str, bytes, str, str]] = []
        self.image_calls: list[tuple[str, str, bytes, str, str]] = []
        self.error = error

    def send_chat(
        self, chat_id: str, text: str, *, idempotency_key: str
    ) -> str:
        self.calls.append((chat_id, text, idempotency_key))
        if self.error is not None:
            raise self.error
        return f"om-route-{len(self.calls)}"

    def send_chat_file(
        self,
        chat_id: str,
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.file_calls.append((chat_id, name, data, media_type, idempotency_key))
        if self.error is not None:
            raise self.error
        return {
            "kind": "file",
            "fileName": name,
            "mediaType": media_type,
            "sizeBytes": len(data),
            "fileType": "stream",
            "fileKey": "file_route_1",
            "nativeMessageId": "om-route-file-1",
        }

    def send_chat_image(
        self,
        chat_id: str,
        name: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self.image_calls.append((chat_id, name, data, media_type, idempotency_key))
        if self.error is not None:
            raise self.error
        return {
            "kind": "image",
            "fileName": name,
            "mediaType": media_type,
            "sizeBytes": len(data),
            "imageKey": "img_route_1",
            "nativeMessageId": "om-route-image-1",
        }


class FakeInbox:
    def __init__(self) -> None:
        self.submitted: list[InboxMessage] = []

    def pending_messages(self, _actor: str) -> tuple[InboxMessage, ...]:
        return ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, _actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)


ROUTES: list[dict[str, object]] = [
    {"name": "allen-dm", "type": "direct", "nativeId": "oc_dm_chat"},
    {"name": "py-test", "type": "direct", "nativeId": "oc_group_chat"},
    {"name": "broadcast", "type": "fanout", "members": ["allen-dm", "py-test"]},
]


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "sender-home"
    (home / "secrets").mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "alicesmith-hyprial-e2e",
                        "appId": "cli_sender_app",
                        "credentialRef": "lark-alicesmith-hyprial-e2e",
                        "routes": ROUTES,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (home / "secrets" / "lark-alicesmith-hyprial-e2e.json").write_text(
        json.dumps({"appSecret": "isolated-sender-secret"}),
        encoding="utf-8",
    )
    return home


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway: FakeChatGateway,
) -> tuple[DaemonApplication, FakeInbox]:
    home = _home(tmp_path)
    state = home / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="sender-mac",
        hyprial_home=home,
        owner="alice",
    **_deep_module_daemon_dependencies())
    inbox = FakeInbox()
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    monkeypatch.setattr(
        LarkSdkGateway, "from_credentials", lambda app_id, secret, **_: gateway
    )
    # resolve-or-reject (Allen b): the sender persona is a registered identity.
    app.handle("agent.create", {"name": "worker"})
    return app, inbox


def _sender(app: DaemonApplication) -> str:
    """The registered sender persona's official address (its id URI)."""

    agent = app.agents.get("worker")
    assert agent is not None
    return agent.uri


def _send(app: DaemonApplication, target: str, **extra: object) -> dict[str, object]:
    return app.handle(
        "message.send",
        {
            "from": _sender(app),
            "to": [target],
            "summary": "kanban broadcast",
            "message": "kanban broadcast",
            "idempotencyKey": "operation-1",
            **extra,
        },
    )
















def test_harness_reply_resource_is_rejected_before_pending_lookup_or_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app, inbox = _application(tmp_path, monkeypatch, gateway)
    attachment = tmp_path / "proof.txt"
    attachment.write_text("not silently accepted", encoding="utf-8")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": _sender(app),
                "messageId": "msg-pending",
                "summary": "reply text",
                "message": "reply text",
                "resourcePaths": [
                    {
                        "path": str(attachment),
                        "kind": "file",
                        "mediaType": "text/plain",
                    }
                ],
            },
        )

    assert failure.value.code == "MESSAGE_REPLY_RESOURCES_UNSUPPORTED"
    assert "not acknowledged" in str(failure.value)
    assert inbox.submitted == []
    assert gateway.calls == []


















def test_missing_app_scope_maps_clickable_admin_action() -> None:
    error = LarkApiError(
        "send chat message",
        230027,
        "permission denied",
        missing_scopes=("im:message.group_msg",),
        authorization_url="https://open.feishu.cn/app/cli_app/permission",
    )
    mapped = map_lark_send_error(
        error, adapter="feishu", route="group", chat_id="oc_group"
    )
    assert mapped.code == "ROUTE_APP_SCOPE_REQUIRED"
    assert mapped.data["missingScopes"] == ["im:message.group_msg"]
    assert mapped.data["authorizationUrl"].startswith("https://open.feishu.cn/")

    platform_permission = map_lark_send_error(
        LarkApiError(
            "send chat message",
            99991679,
            "permission denied",
            missing_scopes=("im:message",),
        ),
        adapter="feishu",
        route="group",
        chat_id="oc_group",
    )
    assert platform_permission.code == "ROUTE_APP_SCOPE_REQUIRED"


def test_missing_larksuite_scope_maps_reason_when_no_url_exists() -> None:
    mapped = map_lark_send_error(
        LarkApiError(
            "send chat message",
            99991679,
            "permission denied",
            missing_scopes=("im:message",),
            authorization_url_reason="larksuite_console_url_missing",
        ),
        adapter="larksuite",
        route="group",
        chat_id="oc_group",
    )

    assert mapped.code == "ROUTE_APP_SCOPE_REQUIRED"
    assert "authorizationUrl" not in mapped.data
    assert mapped.data["authorizationUrlReason"] == (
        "larksuite_console_url_missing"
    )
    assert "authorizationUrlReason=larksuite_console_url_missing" in str(mapped)
    assert "open.feishu.cn/page/scope-apply" not in str(mapped.data)


def test_ambiguous_230027_does_not_claim_scope_recovery() -> None:
    mapped = map_lark_send_error(
        LarkApiError("send", 230027, "ambiguous"),
        adapter="feishu",
        route="group",
        chat_id="oc_group",
    )
    assert mapped.code == "USER_DELIVERY_FAILED"








def test_fanout_member_must_be_a_direct_route() -> None:
    configuration = ChannelConfiguration.from_json(
        {
            "version": 1,
            "gateways": [
                {
                    "provider": "lark",
                    "name": "a",
                    "appId": "cli_a",
                    "credentialRef": "lark-cred",
                    "routes": [
                        {"name": "inner", "type": "fanout", "members": ["leaf"]},
                        {"name": "leaf", "type": "direct", "nativeId": "oc_leaf"},
                        {"name": "outer", "type": "fanout", "members": ["inner"]},
                    ],
                }
            ],
        }
    )

    with pytest.raises(RouteDeliveryError) as failure:
        resolve_gateway_routes(configuration.gateways[0], "outer")

    assert failure.value.code == "ROUTE_FANOUT_MEMBER_INVALID"
    assert "inner" in str(failure.value)


def test_future_scheme_fails_loudly_instead_of_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects future readers: an address form nobody has implemented yet
    (or a typo like ``usr:allen``) must fail at send time, never queue."""

    gateway = FakeChatGateway()
    app, inbox = _application(tmp_path, monkeypatch, gateway)

    for target in ("foo:bar", "usr:allen", "route:a:b:c:d", "route:a:b"):
        with pytest.raises(DaemonRequestError) as failure:
            _send(app, target)
        assert failure.value.code in {"UNSUPPORTED_TARGET", "INVALID_ARGUMENT"}
    assert inbox.submitted == []


def test_channel_scheme_keeps_its_specific_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app, inbox = _application(tmp_path, monkeypatch, gateway)

    with pytest.raises(DaemonRequestError) as failure:
        _send(
            app,
            "channel:alice:mac:feishu-main:hyprial-all-member",
            **{"from": app.agents.get("worker").uri},
        )

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert "route:" not in str(failure.value)
    assert inbox.submitted == []


def test_bare_actor_and_agent_form_still_reach_the_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeChatGateway()
    app, inbox = _application(tmp_path, monkeypatch, gateway)

    result = _send(app, "agent:alice:mac:worker")

    assert result["ok"] is True
    assert len(inbox.submitted) == 1
    assert inbox.submitted[0].recipient == "agent:alice:mac:worker"
    assert gateway.calls == []
