from __future__ import annotations

from hyprial.daemon.impl.adapters.lark.credentials.scopes import diagnose_scopes, request_scope_authorization, scope_apply_url

def test_group_history_requires_group_and_base_read_scope() -> None:
    def status(scopes):  # type: ignore[no-untyped-def]
        result = diagnose_scopes(
            adapter="feishu",
            app_id="app",
            response={
                "code": 0,
                "data": {
                    "scopes": [
                        {"scope_name": scope, "grant_status": 1}
                        for scope in scopes
                    ]
                },
            },
        )
        return next(
            row for row in result["capabilities"] if row["id"] == "group-history"
        )

    missing_base = status({"im:message.group_msg"})
    assert missing_base["status"] == "undeclared"
    assert missing_base["scopeGroups"] == [
        ["im:message.group_msg"],
        ["im:message", "im:message:readonly"],
    ]
    assert missing_base["missingGroups"] == [
        {"anyOf": ["im:message", "im:message:readonly"], "status": "undeclared"}
    ]
    missing_group = status({"im:message:readonly"})
    assert missing_group["missingGroups"] == [
        {"anyOf": ["im:message.group_msg"], "status": "undeclared"}
    ]
    assert status({"im:message.group_msg", "im:message"})["status"] == "granted"


def test_scope_apply_url_encodes_deduplicates_and_preserves_order() -> None:
    assert scope_apply_url(
        "cli id/with?reserved",
        ("im:message", "contact:user.base:readonly", "im:message"),
    ) == (
        "https://open.feishu.cn/page/scope-apply?"
        "clientID=cli+id%2Fwith%3Freserved&"
        "scopes=im%3Amessage%2Ccontact%3Auser.base%3Areadonly"
    )
    assert scope_apply_url("cli_test_app", ()) == (
        "https://open.feishu.cn/app/cli_test_app/permission"
    )


def test_apply_result_codes_are_stable() -> None:
    expected = {
        0: (True, "requested"),
        212001: (False, "super_sensitive_only"),
        212002: (False, "nothing_to_request"),
        212003: (False, "request_limit_exceeded"),
        212004: (True, "already_requested"),
    }
    for code, pair in expected.items():
        result = request_scope_authorization({"code": code, "msg": "result"})
        assert (result["ok"], result["status"]) == pair
