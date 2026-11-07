from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from pathlib import Path
from typer.testing import CliRunner
from hyprial.daemon.impl.adapters.lark.credentials.scopes import diagnose_scopes

from ..support import (
    _configured_home,
    _interactive_authorize,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_doctor_distinguishes_granted_declared_and_undeclared() -> None:
    result = diagnose_scopes(
        adapter="feishu-main",
        app_id="cli_test_app",
        response={
            "code": 0,
            "data": {
                "scopes": [
                    {
                        "scope_name": "im:message",
                        "grant_status": 1,
                        "scope_type": "tenant",
                    },
                    {
                        "scope_name": "im:message.group_at_msg:readonly",
                        "grant_status": 0,
                        "scope_type": "tenant",
                    },
                    {
                        "scope_name": "im:resource",
                        "grant_status": 1,
                        "scope_type": "user",
                    },
                ]
            },
        },
    )
    by_id = {row["id"]: row for row in result["capabilities"]}
    assert by_id["message-send"]["status"] == "granted"
    assert by_id["receive-group-mention"] == {
        "id": "receive-group-mention",
        "requirement": "required",
        "anyOf": ["im:message.group_at_msg:readonly", "im:message.group_at_msg"],
        "status": "declared_ungranted",
        "declaredScopes": ["im:message.group_at_msg:readonly"],
        "action": {
            "type": "request_admin_authorization",
            "command": "hyprial adapter scopes authorize feishu-main",
        },
    }
    assert by_id["message-resources"]["status"] == "undeclared"
    assert by_id["group-history"]["action"]["url"] == (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage.group_msg"
    )


def test_adapter_doctor_and_authorize_use_configured_app_with_fake_port(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, app_id: str, secret: str, **_kwargs: object) -> None:
            assert app_id == "cli_test_app" and secret == "must-not-leak"

        def list_scopes(self):  # type: ignore[no-untyped-def]
            calls.append(("GET", "scopes"))
            return {
                "code": 0,
                "data": {
                    "scopes": [
                        {
                            "scope_name": "im:message",
                            "grant_status": 1,
                            "scope_type": "tenant",
                        },
                    ]
                },
            }

        def apply_scopes(self):  # type: ignore[no-untyped-def]
            calls.append(("POST", "apply"))
            return {"code": 212004, "msg": "already requested"}

    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.scopes.LarkScopeClient", FakeClient)
    runner = CliRunner()
    doctor = runner.invoke(
        _cli_root_app, ["adapter", "scopes", "check", "feishu-main", "--json"]
    )
    authorize = runner.invoke(
        _cli_root_app, ["adapter", "scopes", "authorize", "feishu-main", "--json"]
    )
    # The exit status agrees with ok: undeclared required scopes make doctor ok:false.
    assert doctor.exit_code == (0 if json.loads(doctor.stdout)["ok"] else 1), doctor.output
    assert authorize.exit_code == 0, authorize.output
    assert json.loads(authorize.stdout)["status"] == "already_requested"
    assert json.loads(authorize.stdout)["authorizationUrl"] == (
        "https://open.feishu.cn/app/cli_test_app/permission"
    )
    assert "missing scopes are unknown" in json.loads(authorize.stdout)[
        "authorizationUrlReason"
    ]
    assert "must-not-leak" not in doctor.output + authorize.output
    assert calls == [("GET", "scopes"), ("POST", "apply")]


def test_authorize_scope_generates_preselected_link_without_lark_calls(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))

    class UnexpectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("scope link generation must not build an HTTP client")

    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.scopes.LarkScopeClient", UnexpectedClient)
    runner = CliRunner()
    arguments = [
        "adapter",
        "scopes",
        "authorize",
        "feishu-main",
        "--scope",
        "im:message",
        "--scope",
        "contact:user.base:readonly",
        "--scope",
        "im:message",
    ]
    result = runner.invoke(_cli_root_app, [*arguments, "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["status"] == "link_generated"
    assert body["scopes"] == ["im:message", "contact:user.base:readonly"]
    assert body["authorizationUrl"] == (
        "https://open.feishu.cn/page/scope-apply?clientID=cli_test_app&"
        "scopes=im%3Amessage%2Ccontact%3Auser.base%3Areadonly"
    )
    human = runner.invoke(_cli_root_app, arguments)
    assert human.exit_code == 0, human.output
    assert "open.feishu.cn/page/scope-apply" in human.stdout


def test_authorize_scope_rejects_invalid_names_without_lark_calls(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))

    class UnexpectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("invalid scopes must be rejected before HTTP setup")

    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.scopes.LarkScopeClient", UnexpectedClient)
    runner = CliRunner()
    for invalid in ("", "im: message", "im:message,contact:user"):
        result = runner.invoke(
            _cli_root_app,
            [
                "adapter",
                "scopes",
                "authorize",
                "feishu-main",
                "--scope",
                invalid,
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_authorize_scope_is_separate_from_interactive_and_capability(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    runner = CliRunner()
    for conflicting in (
        ["--interactive"],
        ["--capability", "group-history"],
    ):
        result = runner.invoke(
            _cli_root_app,
            [
                "adapter",
                "scopes",
                "authorize",
                "feishu-main",
                "--scope",
                "im:message",
                *conflicting,
                "--json",
            ],
        )
        assert result.exit_code == 1
        body = json.loads(result.stdout)
        assert body["code"] == "INVALID_ARGUMENT"
        assert "--scope cannot be combined" in body["error"]


def test_authorize_dead_end_names_the_interactive_way_out(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """`nothing_to_request` is the product-visible dead end, not a success.

    The scopes/apply endpoint can only ask for scopes the App already declared,
    so an App missing a *declaration* gets 212002 and no amount of retrying
    helps. The response has to point at the flow that can declare it.
    """

    home = _configured_home(tmp_path)

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def apply_scopes(self):  # type: ignore[no-untyped-def]
            return {"code": 212002, "msg": "nothing to request"}

    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.scopes.LarkScopeClient", FakeClient)
    result = CliRunner().invoke(
        _cli_root_app, ["adapter", "scopes", "authorize", "feishu-main", "--json"]
    )

    assert result.exit_code == 1, result.output  # ok:false exits 1
    body = json.loads(result.stdout)
    assert body["status"] == "nothing_to_request" and body["ok"] is False
    # The existing manual-handoff keys stay put; the hint is additive.
    assert body["authorizationUrl"].endswith("/permission")
    assert body["manualApprovalRequired"] is True
    assert "authorize feishu-main --interactive" in body["nextStep"]


def test_authorize_refuses_capability_without_the_interactive_flow(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "adapter",
            "scopes",
            "authorize",
            "feishu-main",
            "--capability",
            "group-history",
            "--json",
        ],
    )

    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["code"] == "INVALID_ARGUMENT"
    assert "--capability requires --interactive" in body["error"]


def test_interactive_authorize_reauthorizes_the_configured_app(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    invocation, calls = _interactive_authorize(tmp_path, monkeypatch)

    assert invocation.exit_code == 0, invocation.output
    body = json.loads(invocation.stdout)
    assert body["status"] == "authorized" and body["appId"] == "cli_test_app"
    assert body["scopes"] == ["im:message", "im:message.group_at_msg:readonly"]
    assert body["capabilities"] == []
    assert body["verificationUrl"] == "https://accounts.example.invalid/x?d=1"
    assert len(calls) == 1
    # Re-authorization, never creation.
    assert calls[0]["app_id"] == "cli_test_app"
    assert "create_only" not in calls[0] and "app_preset" not in calls[0]
    # The flow authenticates the human, so no app secret is sent or echoed.
    assert "must-not-leak" not in json.dumps(calls[0]) + invocation.output
    # The link is a handoff on stderr; stdout stays exactly one JSON value.
    assert "https://accounts.example.invalid/x?d=1" in invocation.stderr
    assert json.loads(invocation.stdout) == body


def test_interactive_authorize_opts_into_group_history_out_loud(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    invocation, calls = _interactive_authorize(
        tmp_path, monkeypatch, "--capability", "group-history"
    )

    assert invocation.exit_code == 0, invocation.output
    body = json.loads(invocation.stdout)
    assert body["capabilities"] == ["group-history"]
    assert calls[0]["addons"]["scopes"]["tenant"] == [
        "im:message",
        "im:message.group_at_msg:readonly",
        "im:message.group_msg",
    ]
    # A privacy-widening scope must be named where the human reads the link,
    # not only inside an encoded URL parameter.
    assert "im:message.group_msg" in invocation.stderr
    assert "every message in the groups" in invocation.stderr


def test_interactive_authorize_without_a_capability_stays_narrow(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    invocation, calls = _interactive_authorize(tmp_path, monkeypatch)

    assert invocation.exit_code == 0, invocation.output
    assert "im:message.group_msg" not in json.dumps(calls[0]["addons"])
    assert "im:message.group_msg" not in invocation.output


def test_interactive_authorize_rejects_an_unknown_capability(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    invocation, calls = _interactive_authorize(
        tmp_path, monkeypatch, "--capability", "group-hisotry"
    )

    assert invocation.exit_code == 1
    body = json.loads(invocation.stdout)
    assert body["code"] == "INVALID_ARGUMENT"
    assert "unknown Lark gateway capability" in body["error"]
    # Rejected before anyone is asked to approve anything.
    assert calls == []


def test_interactive_authorize_refuses_a_different_app(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    invocation, _ = _interactive_authorize(
        tmp_path,
        monkeypatch,
        result={"client_id": "cli_other_app", "client_secret": "nope"},
    )

    assert invocation.exit_code == 1
    body = json.loads(invocation.stdout)
    assert body["code"] == "LARK_AUTHORIZATION_FAILED"
    assert "cli_other_app" in body["error"]


def test_interactive_authorize_reports_a_missing_adapter(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    home = _configured_home(tmp_path)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "adapter",
            "scopes",
            "authorize",
            "not-configured",
            "--interactive",
            "--json",
        ],
    )

    assert result.exit_code == 1
    body = json.loads(result.stdout)
    # The adapter must already exist; this path never creates or names an App
    # for it, so a typo has to surface as a lookup failure.
    assert body["code"] == "ADAPTER_NOT_FOUND"


def test_v6_apply_uses_bearer_token_and_a_truly_empty_post(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    requests = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._stream = BytesIO(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size: int = -1):
            return self._stream.read(size)

    def open_request(request, timeout):  # type: ignore[no-untyped-def]
        assert timeout == 15
        requests.append(request)
        if request.full_url.endswith("tenant_access_token/internal"):
            return Response(b'{"code":0,"tenant_access_token":"tenant-secret"}')
        assert request.full_url.endswith("application/v6/scopes/apply")
        assert request.method == "POST" and request.data is None
        assert request.headers["Authorization"] == "Bearer tenant-secret"
        return Response(b'{"code":0,"msg":"success"}')

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import LarkScopeClient

    result = LarkScopeClient(
        "cli_test", "app-secret", origin="https://fake.invalid"
    ).apply_scopes()
    assert result["code"] == 0 and len(requests) == 2


def test_http_error_business_json_is_bounded_and_secret_safe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import LarkScopeClient

    def fail(payload: bytes):  # type: ignore[no-untyped-def]
        def open_request(_request, timeout):  # type: ignore[no-untyped-def]
            assert timeout == 15
            raise urllib.error.HTTPError(
                "https://open.feishu.cn/redacted",
                400,
                "bad request",
                {},
                BytesIO(payload),
            )

        monkeypatch.setattr("urllib.request.urlopen", open_request)

    client = LarkScopeClient("app", "secret")
    fail(b'{"code":212003,"msg":"request limit"}')
    assert client._json("/apply", method="POST") == {
        "code": 212003,
        "msg": "request limit",
    }

    for payload in (b"not-json token=secret", b"{" + b"x" * 70_000):
        fail(payload)
        try:
            client._json("/apply", method="POST")
        except RuntimeError as error:
            assert "secret" not in str(error)
            assert str(error) in {
                "Lark scope request failed: JSONDecodeError",
                "Lark scope request failed: RuntimeError",
            }
        else:
            raise AssertionError("unsafe HTTP error body was accepted")
