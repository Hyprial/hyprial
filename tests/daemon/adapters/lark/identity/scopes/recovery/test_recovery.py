from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from hyprial.daemon.impl.adapters.lark.credentials.scopes import LARK_GATEWAY_CAPABILITIES, LarkScopeThrottleStore, LarkScopeRecovery, parse_permission_violation

def test_manifest_includes_group_history_recovery_scope() -> None:
    history = next(
        row for row in LARK_GATEWAY_CAPABILITIES if row.id == "group-history"
    )
    assert history.scope_groups == (
        ("im:message.group_msg",),
        ("im:message", "im:message:readonly"),
    )


def test_permission_violation_requires_structured_scope_evidence() -> None:
    payload = {
        "code": 230027,
        "error": {
            "permission_violations": [
                {"subject": "im:message.group_msg"},
                {"subject": "../../not-a-scope"},
                {"subject": "im:message.group_msg"},
            ],
            "console_url": "https://evil.example/permission",
        },
    }
    assert parse_permission_violation(payload) == {"scopes": ["im:message.group_msg"]}
    assert parse_permission_violation({"code": 230027, "msg": "ambiguous"}) is None
    assert parse_permission_violation({"code": 99991679, "error": {}}) is None


def test_permission_violation_accepts_only_official_clickable_link() -> None:
    payload = {
        "response": {
            "data": {
                "code": 99991679,
                "error": {
                    "permission_violations": [{"subject": "im:message"}],
                    "console_url": "https://open.feishu.cn/opaque?a=1%202",
                },
            }
        }
    }
    assert parse_permission_violation(payload) == {
        "scopes": ["im:message"],
        "consoleUrl": "https://open.feishu.cn/opaque?a=1%202",
    }

    for unsafe in (
        "https://user:password@open.feishu.cn/permission",
        "https://open.feishu.cn:444/permission",
        "https://open.feishu.cn.evil.example/permission",
        "http://open.feishu.cn/permission",
    ):
        payload["response"]["data"]["error"]["console_url"] = unsafe
        assert parse_permission_violation(payload) == {"scopes": ["im:message"]}

    payload["response"]["data"]["error"]["console_url"] = (
        "https://open.feishu.cn/permission?tenant=x#untrusted-fragment"
    )
    assert parse_permission_violation(payload) == {
        "scopes": ["im:message"],
        "consoleUrl": "https://open.feishu.cn/permission?tenant=x",
    }


def test_permission_violation_reads_real_sdk_help_links() -> None:
    from lark_oapi.core.model.error import Error

    error = Error(
        {
            "permission_violations": [{"subject": "im:message"}],
            "helps": [
                {"url": "https://evil.example/permission"},
                {"url": "https://open.larksuite.com/permission#discard"},
            ],
        }
    )
    assert parse_permission_violation(
        {"code": 99991679, "error": error}
    ) == {
        "scopes": ["im:message"],
        "consoleUrl": "https://open.larksuite.com/permission",
    }


def test_recovery_applies_once_per_app_per_hour_and_returns_link() -> None:
    now = [1000.0]
    calls: list[str] = []
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: calls.append("apply") or {"code": 0, "msg": "ok"},
        now=lambda: now[0],
    )
    error = {
        "code": 230027,
        "error": {"permission_violations": [{"subject": "im:message.group_msg"}]},
    }
    first = recovery.handle(error)
    second = recovery.handle(error)
    now[0] += 3600
    third = recovery.handle(error)
    assert first is not None and first["status"] == "requested"
    assert first["authorizationUrl"] == (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage.group_msg"
    )
    assert second == {
        "handled": True,
        "status": "throttled",
        "scopes": ["im:message.group_msg"],
    }
    assert third is not None and third["status"] == "requested"
    assert calls == ["apply", "apply"]


def test_recovery_preserves_larksuite_console_url_after_success() -> None:
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
    )
    result = recovery.handle(
        {
            "code": 99991679,
            "error": {
                "permission_violations": [{"subject": "im:message"}],
                "console_url": "https://open.larksuite.com/opaque-permission",
            },
        }
    )

    assert result is not None
    assert result["authorizationUrl"] == (
        "https://open.larksuite.com/opaque-permission"
    )


def test_recovery_replaces_feishu_console_url_with_preselected_link() -> None:
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
    )
    result = recovery.handle(
        SimpleNamespace(
            missing_scopes=("im:message",),
            authorization_url="https://open.feishu.cn/opaque-permission",
        )
    )

    assert result is not None
    assert result["authorizationUrl"] == (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage"
    )


def test_recovery_larksuite_without_console_url_reports_specific_reason(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "https://open.larksuite.com")
    notifications: list[str] = []
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
        notify=notifications.append,
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )

    assert result is not None
    assert "authorizationUrl" not in result
    assert result["authorizationUrlReason"] == "larksuite_console_url_missing"
    assert "open.feishu.cn/page/scope-apply" not in json.dumps(result)
    assert notifications == [
        "缺少 im:message 权限；未生成预选权限页"
        "（authorizationUrlReason=larksuite_console_url_missing）"
    ]


def test_recovery_larksuite_apply_failure_reports_reason_without_url(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "https://open.larksuite.com")
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )

    assert result is not None
    assert "authorizationUrl" not in result
    assert result["authorizationUrlReason"] == "larksuite_console_url_missing"
    assert result["errorType"] == "RuntimeError"
    assert "open.feishu.cn/page/scope-apply" not in json.dumps(result)


def test_recovery_larksuite_keeps_feishu_console_without_making_scope_apply(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "https://open.larksuite.com")
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
    )
    result = recovery.handle(
        SimpleNamespace(
            missing_scopes=("im:message",),
            authorization_url="https://open.feishu.cn/opaque-permission",
        )
    )

    assert result is not None
    assert result["authorizationUrl"] == "https://open.feishu.cn/opaque-permission"
    assert "open.feishu.cn/page/scope-apply" not in json.dumps(result)


def test_recovery_local_base_uses_intentional_feishu_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "http://127.0.0.1:8931")
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )

    assert result is not None
    assert result["authorizationUrl"] == (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage"
    )


def test_recovery_does_not_treat_feishu_prefix_attack_as_feishu(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", "https://open.larksuite.com")
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 0, "msg": "ok"},
    )
    result = recovery.handle(
        {
            "code": 99991679,
            "error": {
                "permission_violations": [{"subject": "im:message"}],
                "console_url": "https://open.feishu.cn.evil.example/permission",
            },
        }
    )

    assert result is not None
    assert "authorizationUrl" not in result
    assert result["authorizationUrlReason"] == "larksuite_console_url_missing"
    assert "open.feishu.cn/page/scope-apply" not in json.dumps(result)


def test_recovery_does_not_guess_when_scope_evidence_is_missing() -> None:
    calls: list[str] = []
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: calls.append("apply") or {"code": 0, "msg": "ok"},
    )

    assert recovery.handle({"code": 230027, "msg": "ambiguous"}) is None
    assert calls == []


def test_recovery_notifies_once_and_isolates_notification_failure() -> None:
    notifications: list[str] = []
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: {"code": 212004, "msg": "duplicate"},
        notify=lambda text: (
            notifications.append(text)
            or (_ for _ in ()).throw(RuntimeError("notification permission denied"))
        ),
    )
    result = recovery.handle(
        {
            "code": 99991679,
            "error": {"permission_violations": [{"subject": "im:message"}]},
        }
    )
    assert result is not None and result["status"] == "already_requested"
    assert len(notifications) == 1
    assert (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage"
    ) in notifications[0]


def test_recovery_apply_exception_is_sanitized_and_does_not_escape(
    tmp_path: Path,
) -> None:
    notifications: list[str] = []
    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: (_ for _ in ()).throw(RuntimeError("token=do-not-leak")),
        notify=notifications.append,
        throttle=LarkScopeThrottleStore(tmp_path / "scope.json"),
    )
    result = recovery.handle(
        SimpleNamespace(
            missing_scopes=("im:message",),
            authorization_url="https://open.larksuite.com/exception-permission",
        )
    )
    assert result == {
        "handled": True,
        "status": "failed",
        "scopes": ["im:message"],
        "authorizationUrl": "https://open.larksuite.com/exception-permission",
        "errorType": "RuntimeError",
    }
    assert "do-not-leak" not in json.dumps(result)
    persisted = (tmp_path / "scope.json").read_text()
    assert "do-not-leak" not in persisted
    assert "cli_test_app" not in persisted
    assert '"errorType":"RuntimeError"' in persisted
    assert len(notifications) == 1
    assert "自动发起管理员授权申请失败" in notifications[0]
    assert "https://open.larksuite.com/exception-permission" in notifications[0]
    assert "do-not-leak" not in notifications[0]


def test_recovery_apply_and_record_failures_still_notify_once_safely() -> None:
    notifications: list[str] = []

    class RecordFailure:
        def claim(self, *_args):  # type: ignore[no-untyped-def]
            return True

        def record(self, *_args):  # type: ignore[no-untyped-def]
            raise OSError("token=record-secret")

    recovery = LarkScopeRecovery(
        app_id="cli_test_app",
        apply=lambda: (_ for _ in ()).throw(RuntimeError("token=apply-secret")),
        notify=notifications.append,
        throttle=RecordFailure(),  # type: ignore[arg-type]
    )
    result = recovery.handle(
        SimpleNamespace(missing_scopes=("im:message",), authorization_url=None)
    )

    assert result is not None and result["status"] == "failed"
    assert result["persistenceWarning"] == "record:OSError"
    assert result["duplicateApplyRisk"] is True
    assert len(notifications) == 1
    assert "自动发起管理员授权申请失败" in notifications[0]
    assert "scope-apply" in notifications[0]
    serialized = json.dumps({"result": result, "notifications": notifications})
    assert "apply-secret" not in serialized
    assert "record-secret" not in serialized


def test_daemon_permission_notification_uses_a_recovery_free_gateway(
    tmp_path: Path, monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    from hyprial.daemon.impl import application
    from hyprial.daemon.impl.application.messaging.delivery import lark as gateway_provider

    gateways = []

    class FakeGateway:
        def __init__(self) -> None:
            self.recovery = None
            self.sent = []

        def set_permission_recovery(self, recovery):  # type: ignore[no-untyped-def]
            self.recovery = recovery

        def send_chat(self, native_id, text, *, idempotency_key):  # type: ignore[no-untyped-def]
            self.sent.append((native_id, text, idempotency_key))

    def gateway_from_credentials(_app_id, _secret):  # type: ignore[no-untyped-def]
        gateway = FakeGateway()
        gateways.append(gateway)
        return gateway

    class FakeScopeClient:
        def __init__(self, _app_id, _secret):  # type: ignore[no-untyped-def]
            pass

        def apply_scopes(self):  # type: ignore[no-untyped-def]
            return {"code": 0, "msg": "ok"}

    monkeypatch.setattr(
        gateway_provider.LarkSdkGateway,
        "from_credentials",
        gateway_from_credentials,
    )
    monkeypatch.setattr(gateway_provider, "LarkScopeClient", FakeScopeClient)
    config = SimpleNamespace(
        app_id="cli_test_app",
        default_route="dm",
        routes=(SimpleNamespace(name="dm", type="direct", native_id="oc_test"),),
    )

    primary = application.DaemonApplication._lark_gateway_with_scope_recovery(
        config, "secret", tmp_path
    )

    assert primary is gateways[0]
    assert primary.recovery is not None
    assert gateways[1].recovery is None
    primary.recovery(
        SimpleNamespace(
            missing_scopes=("im:message",),
            authorization_url="https://open.feishu.cn/permission",
        )
    )
    assert len(gateways[1].sent) == 1
