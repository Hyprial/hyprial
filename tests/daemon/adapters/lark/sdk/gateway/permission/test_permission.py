from __future__ import annotations

import json
from types import SimpleNamespace
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageResponse
from hyprial.daemon.impl.adapters.lark import LarkApiError, LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.credentials.scopes import LarkScopeRecovery

from ..support import (
    FakeClient,
    install_get_items,
    merge_forward_get_items,
)


def test_sdk_permission_error_preserves_safe_scope_evidence_and_runs_recovery() -> None:
    from lark_oapi.core.model.error import Error

    client = FakeClient()
    recovery_calls = []
    client.message.create = lambda _request: SimpleNamespace(
        success=lambda: False,
        code=230027,
        msg="permission denied",
        error=Error(
            {
                "permission_violations": [{"subject": "im:message.group_msg"}],
                "helps": [
                    {"url": "https://open.feishu.cn/opaque-permission"}
                ],
            }
        ),
    )
    gateway = LarkSdkGateway(
        client,
        app_id="cli_test_app",
        permission_recovery=lambda error: (
            recovery_calls.append(error) or {"status": "requested"}
        ),
    )

    try:
        gateway.send_chat("oc_test", "text", idempotency_key="op")
    except LarkApiError as error:
        assert error.missing_scopes == ("im:message.group_msg",)
        assert error.authorization_url == (
            "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
            "scopes=im%3Amessage.group_msg"
        )
        assert error.recovery == {"status": "requested"}
    else:
        raise AssertionError("permission error was not raised")
    assert len(recovery_calls) == 1


def test_sdk_permission_error_preserves_larksuite_console_url() -> None:
    from lark_oapi.core.model.error import Error

    client = FakeClient()
    client.message.create = lambda _request: SimpleNamespace(
        success=lambda: False,
        code=230027,
        msg="permission denied",
        error=Error(
            {
                "permission_violations": [{"subject": "im:message"}],
                "helps": [
                    {"url": "https://open.larksuite.com/tenant-permission"}
                ],
            }
        ),
    )
    gateway = LarkSdkGateway(client, app_id="cli_test_app")

    try:
        gateway.send_chat("oc_test", "text", idempotency_key="larksuite")
    except LarkApiError as error:
        assert error.missing_scopes == ("im:message",)
        assert error.authorization_url == (
            "https://open.larksuite.com/tenant-permission"
        )
    else:
        raise AssertionError("permission error was not raised")


def test_sdk_larksuite_without_console_url_carries_specific_reason(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    from lark_oapi.core.model.error import Error

    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "https://open.larksuite.com")
    client = FakeClient()
    client.message.create = lambda _request: SimpleNamespace(
        success=lambda: False,
        code=230027,
        msg="permission denied",
        error=Error(
            {"permission_violations": [{"subject": "im:message"}], "helps": []}
        ),
    )
    gateway = LarkSdkGateway(client, app_id="cli_test_app")

    try:
        gateway.send_chat("oc_test", "text", idempotency_key="larksuite-no-link")
    except LarkApiError as error:
        assert error.missing_scopes == ("im:message",)
        assert error.authorization_url is None
        assert error.authorization_url_reason == "larksuite_console_url_missing"
    else:
        raise AssertionError("permission error was not raised")


def test_sdk_permission_error_from_real_172_json_model_runs_recovery() -> None:
    """lark-oapi 1.7.2 leaves CreateMessageResponse.error as a dict."""

    for code in (230027, 99991679):
        response_model = lark.JSON.unmarshal(
            json.dumps(
                {
                    "code": code,
                    "msg": "permission denied",
                    "error": {
                        "permission_violations": [
                            {"subject": "im:message.group_msg"}
                        ],
                        "helps": [
                            {"url": "https://open.feishu.cn/opaque-permission"}
                        ],
                    },
                }
            ),
            CreateMessageResponse,
        )
        assert isinstance(response_model.error, dict)
        client = FakeClient()
        client.message.create = lambda _request, item=response_model: item
        recovery_calls: list[LarkApiError] = []
        gateway = LarkSdkGateway(
            client,
            permission_recovery=lambda error: (
                recovery_calls.append(error) or {"status": "requested"}
            ),
        )

        try:
            gateway.send_chat("oc_test", "text", idempotency_key=f"op-{code}")
        except LarkApiError as error:
            assert error.code == code
            assert error.missing_scopes == ("im:message.group_msg",)
            assert error.authorization_url == (
                "https://open.feishu.cn/opaque-permission"
            )
            assert error.recovery == {"status": "requested"}
        else:
            raise AssertionError("permission error was not raised")
        assert len(recovery_calls) == 1
        assert recovery_calls[0].code == code


def test_sdk_recovery_failure_never_replaces_original_lark_error() -> None:
    client = FakeClient()
    client.message.create = lambda _request: SimpleNamespace(
        success=lambda: False,
        code=99991679,
        msg="permission denied",
        error=SimpleNamespace(
            permission_violations=[SimpleNamespace(subject="im:message")],
            helps=[],
        ),
    )
    gateway = LarkSdkGateway(
        client,
        permission_recovery=lambda _error: (_ for _ in ()).throw(
            RuntimeError("token=must-not-leak")
        ),
    )

    try:
        gateway.send_chat("oc_test", "text", idempotency_key="op")
    except LarkApiError as error:
        assert error.code == 99991679
        assert error.recovery == {
            "handled": True,
            "status": "failed",
            "errorType": "RuntimeError",
        }
        assert "must-not-leak" not in str(error.recovery)
    else:
        raise AssertionError("original LarkApiError was replaced")


def test_sdk_apply_failure_returns_original_lark_error_and_notifies_safely() -> None:
    response_model = lark.JSON.unmarshal(
        json.dumps(
            {
                "code": 230027,
                "msg": "permission denied",
                "error": {
                    "permission_violations": [{"subject": "im:message"}],
                    "helps": [
                        {"url": "https://open.feishu.cn/opaque-permission"}
                    ],
                },
            }
        ),
        CreateMessageResponse,
    )
    client = FakeClient()
    client.message.create = lambda _request: response_model
    notifications: list[str] = []

    class RecordFailure:
        def claim(self, *_args):  # type: ignore[no-untyped-def]
            return True

        def record(self, *_args):  # type: ignore[no-untyped-def]
            raise OSError("token=record-secret")

    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: (_ for _ in ()).throw(RuntimeError("token=must-not-leak")),
        notify=notifications.append,
        throttle=RecordFailure(),  # type: ignore[arg-type]
    )
    gateway = LarkSdkGateway(client, permission_recovery=recovery.handle)

    try:
        gateway.send_chat("oc_test", "text", idempotency_key="apply-failure")
    except LarkApiError as error:
        assert error.code == 230027
        assert error.missing_scopes == ("im:message",)
        assert error.recovery is not None
        assert error.recovery["status"] == "failed"
        assert error.recovery["errorType"] == "RuntimeError"
        assert error.recovery["persistenceWarning"] == "record:OSError"
        assert error.recovery["duplicateApplyRisk"] is True
        assert "must-not-leak" not in json.dumps(error.recovery)
        assert "record-secret" not in json.dumps(error.recovery)
    else:
        raise AssertionError("original LarkApiError was replaced")
    assert len(notifications) == 1
    assert "must-not-leak" not in notifications[0]
    assert "record-secret" not in notifications[0]


def test_get_inbound_message_expands_merge_forward_for_recovery() -> None:
    """The recovery path fetches the same response; it discarded items[1:]
    identically, so a recovered forward reached the agent as a label."""

    client = FakeClient()
    install_get_items(client, merge_forward_get_items())
    gateway = LarkSdkGateway(client)

    message = gateway.get_inbound_message("om_forward_shell", chat_type="p2p")

    assert message is not None
    assert message.message_type == "merge_forward"
    assert message.content_status == "supported"
    assert "rolled back to the previous build" in message.text
    # The forward belongs to the chat it landed in, never the source chat the
    # children were copied from.
    assert message.chat_id == "oc_dm"
    assert message.conversation_id == "lark:oc_dm:oc_dm"
