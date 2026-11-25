"""The onboarding handler bindings project declared params onto one request port.

These tests never start a daemon, never open a home and never touch the
production registry.  They prove the binding is a projection only: the admitted
``CallContext`` comes from ``app.onboarding_context`` before any port call,
request params reach the port with exactly their declared names, the port's
declared failure codes survive, undeclared internal codes and arbitrary
exceptions never leak, and no handler runs the post-reply action.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application.onboarding.errors import (
    OnboardingGatewayError,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    CallContext,
    Surface,
)
from hyprial.daemon.impl.request_catalog.onboarding.handlers import (
    ONBOARDING_BINDINGS,
    auth_login_begin,
    auth_logout,
    auth_operation_cancel,
    auth_operation_get,
    auth_relogin,
    onboarding_device_ensure,
    onboarding_first_run_begin,
    onboarding_first_run_bind,
    onboarding_first_run_cancel,
    onboarding_first_run_confirm,
    onboarding_first_run_skip,
    onboarding_initialize,
    onboarding_status,
)
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    AUTH_OPERATION_CONFLICT,
    ONBOARDING_SPECS,
    REGISTRATION_UNAVAILABLE,
)
from hyprial.kernel import DaemonRequestError, ipc_errors

SURFACE = Surface("home-binding", "host-control", "view", "generation-1")
CONTEXT = CallContext(
    surface=SURFACE,
    phase="bootstrap",
    caller="local-bootstrap",
    owner=None,
)


class FakePort:
    """Record every port call; ``behavior`` injects one exact outcome."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.post_reply_calls = 0
        self.behavior: dict[str, object] = {}

    def _call(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))
        outcome = self.behavior.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return {} if outcome is None else outcome

    def status(self, context):
        return self._call("status", (context,), {})

    def begin_auth(self, context, **kwargs):
        return self._call("begin_auth", (context,), kwargs)

    def get(self, context, **kwargs):
        return self._call("get", (context,), kwargs)

    def cancel(self, context, **kwargs):
        return self._call("cancel", (context,), kwargs)

    def ensure_device(self, context, **kwargs):
        return self._call("ensure_device", (context,), kwargs)

    def initialize(self, context, **kwargs):
        return self._call("initialize", (context,), kwargs)

    def logout(self, context, **kwargs):
        return self._call("logout", (context,), kwargs)

    def relogin(self, context, **kwargs):
        return self._call("relogin", (context,), kwargs)

    def take_post_reply(self):
        self.post_reply_calls += 1
        return None


class FakeApp:
    def __init__(
        self,
        port,
        context=CONTEXT,
        *,
        context_error: BaseException | None = None,
        context_type: object | None = None,
    ) -> None:
        self._port = port
        self._context = context
        self._context_error = context_error
        self._context_type = context_type
        self.origins: list[str | None] = []
        self.logs: list[tuple[str, str, str, dict[str, object]]] = []

    def _log(self, level, component, event, **fields):
        self.logs.append((level, component, event, fields))

    @property
    def onboarding(self):
        if self._port is None:
            raise AttributeError("onboarding")
        return self._port

    def onboarding_context(self, *, trusted_message_origin):
        self.origins.append(trusted_message_origin)
        if self._context_error is not None:
            raise self._context_error
        if self._context_type is not None:
            return self._context_type
        return self._context


def test_bindings_cover_all_declared_specs_in_declaration_order():
    assert [spec.name for spec, _handler in ONBOARDING_BINDINGS] == [
        spec.name for spec in ONBOARDING_SPECS
    ]
    assert dict(
        (spec.name, handler) for spec, handler in ONBOARDING_BINDINGS
    ) == {
        "onboarding.status": onboarding_status,
        "auth.login.begin": auth_login_begin,
        "auth.operation.get": auth_operation_get,
        "auth.operation.cancel": auth_operation_cancel,
        "onboarding.device.ensure": onboarding_device_ensure,
        "onboarding.initialize": onboarding_initialize,
        "auth.logout": auth_logout,
        "auth.relogin": auth_relogin,
        "onboarding.firstRun.begin": onboarding_first_run_begin,
        "onboarding.firstRun.bind": onboarding_first_run_bind,
        "onboarding.firstRun.confirm": onboarding_first_run_confirm,
        "onboarding.firstRun.cancel": onboarding_first_run_cancel,
        "onboarding.firstRun.skip": onboarding_first_run_skip,
    }


def test_status_projects_the_admitted_context_and_returns_the_port_result():
    result = {"ok": True, "phase": "bootstrap"}
    port = FakePort()
    port.behavior["status"] = result
    app = FakeApp(port)

    assert onboarding_status(app, {}, trusted_message_origin="ipc") is result
    assert port.calls == [("status", (CONTEXT,), {})]
    assert app.origins == ["ipc"]
    assert port.post_reply_calls == 0


def test_login_begin_forwards_declared_wire_names_as_keyword_only_ids():
    port = FakePort()
    app = FakeApp(port)

    auth_login_begin(
        app,
        {"intent": "login", "operationKey": "opkey-0123456789abcdef"},
        trusted_message_origin=None,
    )

    assert port.calls == [
        (
            "begin_auth",
            (CONTEXT,),
            {"intent": "login", "operation_key": "opkey-0123456789abcdef"},
        )
    ]


def test_until_serving_context_failure_uses_the_handler_error_boundary():
    port = FakePort()
    app = FakeApp(port, context_error=RuntimeError("private context failure"))

    with pytest.raises(DaemonRequestError) as refused:
        auth_login_begin(
            app,
            {
                "intent": "login",
                "operationKey": "opkey-0123456789abcdef",
                "until": "serving",
            },
        )

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert str(refused.value) == "auth.login.begin failed"
    assert len(app.logs) == 1


def test_get_and_cancel_forward_the_operation_id():
    port = FakePort()
    app = FakeApp(port)

    auth_operation_get(app, {"operationId": "op-1"})
    auth_operation_cancel(app, {"operationId": "op-2"})

    assert port.calls == [
        ("get", (CONTEXT,), {"operation_id": "op-1"}),
        ("cancel", (CONTEXT,), {"operation_id": "op-2"}),
    ]


def test_device_and_initialize_forward_the_operation_key():
    port = FakePort()
    app = FakeApp(port)

    onboarding_device_ensure(app, {"operationKey": "device-key"})
    onboarding_initialize(app, {"operationKey": "initialize-key"})

    assert port.calls == [
        ("ensure_device", (CONTEXT,), {"operation_key": "device-key"}),
        ("initialize", (CONTEXT,), {"operation_key": "initialize-key"}),
    ]


def test_missing_port_is_daemon_not_ready_and_no_context_is_asked_for():
    app = FakeApp(None)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert app.origins == []


def test_missing_admitted_frame_refuses_before_any_port_call():
    port = FakePort()
    app = FakeApp(
        port,
        context_error=DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "no admitted control frame is attached to this request",
        ),
    )

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert port.calls == []


def test_a_non_context_result_never_reaches_the_port():
    port = FakePort()
    app = FakeApp(port, context_type={"phase": "bootstrap"})

    with pytest.raises(DaemonRequestError) as refused:
        auth_operation_get(app, {"operationId": "op-1"})

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert port.calls == []


def test_declared_gateway_code_is_preserved_on_the_wire():
    port = FakePort()
    port.behavior["begin_auth"] = OnboardingGatewayError(
        REGISTRATION_UNAVAILABLE,
        "local account registration is not available",
    )
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        auth_login_begin(
            app, {"intent": "register", "operationKey": "opkey-register"}
        )

    assert refused.value.code == REGISTRATION_UNAVAILABLE
    assert refused.value.code in {
        error.code for error in ONBOARDING_SPECS[1].errors
    }
    assert refused.value.data is None
    assert app.logs == []


def test_declared_caller_refusal_is_preserved():
    port = FakePort()
    port.behavior["ensure_device"] = OnboardingGatewayError(
        ipc_errors.CALLER_NOT_AUTHORIZED,
        "onboarding.device.ensure is bootstrap-only",
    )
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_device_ensure(app, {"operationKey": "device-key"})

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert app.logs == []


def test_undeclared_internal_code_is_downgraded_without_detail():
    port = FakePort()
    port.behavior["initialize"] = OnboardingGatewayError(
        "BOOTSTRAP_OPERATION_CONFLICT",
        "internal receipt conflict detail",
        {"accessToken": "secret-token"},
    )
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_initialize(app, {"operationKey": "initialize-key"})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert "BOOTSTRAP_OPERATION_CONFLICT" not in {
        error.code for error in ONBOARDING_SPECS[5].errors
    }
    assert str(refused.value) == "onboarding.initialize failed"
    assert "internal receipt conflict" not in str(refused.value)
    assert len(app.logs) == 1
    level, component, event, fields = app.logs[0]
    assert (level, component, event) == (
        "error",
        "daemon",
        "onboarding.handler_failed",
    )
    assert fields["method"] == "onboarding.initialize"
    assert fields["errorType"] == "OnboardingGatewayError"
    assert fields["detail"] == "onboarding handler failed"
    assert fields["code"] == "BOOTSTRAP_OPERATION_CONFLICT"
    assert "errorMessage" not in fields
    assert "errorTraceback" not in fields
    assert "accessToken" not in str(fields)
    assert "secret-token" not in str(fields)


def test_arbitrary_exception_never_leaks_its_message():
    port = FakePort()
    port.behavior["status"] = RuntimeError(
        "credential path /secret Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature "
        "accessToken=secret-token sk-ant-api03-1234567890123456"
    )
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert str(refused.value) == "onboarding.status failed"
    assert "/secret" not in str(refused.value)
    assert len(app.logs) == 1
    level, component, event, fields = app.logs[0]
    assert (level, component, event) == (
        "error",
        "daemon",
        "onboarding.handler_failed",
    )
    assert fields["method"] == "onboarding.status"
    assert fields["errorType"] == "RuntimeError"
    assert fields["detail"] == "onboarding handler failed"
    assert "errorMessage" not in fields
    assert "errorTraceback" not in fields
    assert "/secret" not in str(fields)
    assert "Bearer" not in str(fields)
    assert "secret-token" not in str(fields)
    assert "sk-ant-api03" not in str(fields)


def test_failure_log_records_a_bounded_exception_chain_without_text():
    cause = RuntimeError("credential path /secret Bearer secret-token")
    try:
        try:
            raise cause
        except RuntimeError as root:
            raise ValueError(
                "device failure",
                {"deviceCode": "device-secret", "accessToken": "secret-token"},
            ) from root
    except ValueError as outcome:
        port = FakePort()
        port.behavior["status"] = outcome
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    fields = app.logs[0][3]
    assert [item["errorType"] for item in fields["errorChain"]] == [
        "ValueError",
        "RuntimeError",
    ]
    assert len(fields["errorChain"]) <= 8
    assert all(len(str(item)) <= 512 for item in fields["errorChain"])
    assert "/secret" not in str(fields)
    assert "device-secret" not in str(fields)
    assert "secret-token" not in str(fields)


def test_declared_daemon_error_logs_its_cause_and_keeps_wire_error():
    cause = RuntimeError("internal credential path /secret")
    try:
        raise OnboardingGatewayError(
            ipc_errors.DAEMON_ERROR, "internal failure"
        ) from cause
    except OnboardingGatewayError as outcome:
        port = FakePort()
        port.behavior["status"] = outcome
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert str(refused.value) == "internal failure"
    assert len(app.logs) == 1
    fields = app.logs[0][3]
    assert fields["code"] == ipc_errors.DAEMON_ERROR
    assert [item["errorType"] for item in fields["errorChain"]] == [
        "OnboardingGatewayError",
        "RuntimeError",
    ]
    assert "/secret" not in str(fields)


def test_failure_wire_error_survives_an_app_without_log(monkeypatch):
    monkeypatch.delattr(FakeApp, "_log")
    port = FakePort()
    port.behavior["status"] = RuntimeError("private failure")
    app = FakeApp(port)

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert str(refused.value) == "onboarding.status failed"


def test_failure_wire_error_survives_a_failing_log_sink():
    port = FakePort()
    port.behavior["status"] = RuntimeError("private failure")
    app = FakeApp(port)

    def broken_log(*_args, **_kwargs):
        raise OSError("log sink unavailable")

    app._log = broken_log

    with pytest.raises(DaemonRequestError) as refused:
        onboarding_status(app, {})

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert str(refused.value) == "onboarding.status failed"


def test_handlers_never_run_the_post_reply_action():
    port = FakePort()
    app = FakeApp(port)

    onboarding_status(app, {})
    auth_login_begin(app, {"intent": "login", "operationKey": "opkey-1"})
    auth_operation_get(app, {"operationId": "op-1"})
    auth_operation_cancel(app, {"operationId": "op-1"})
    onboarding_device_ensure(app, {"operationKey": "device-key"})
    onboarding_initialize(app, {"operationKey": "initialize-key"})
    auth_logout(app, {"operationKey": "logout-key", "confirm": True})
    auth_relogin(app, {"operationKey": "relogin-key"})

    assert port.post_reply_calls == 0
    assert [name for name, _args, _kwargs in port.calls] == [
        "status",
        "begin_auth",
        "get",
        "cancel",
        "ensure_device",
        "initialize",
        "logout",
        "relogin",
    ]


def test_logout_and_relogin_forward_only_declared_wire_fields():
    port = FakePort()
    app = FakeApp(port)

    auth_logout(
        app, {"operationKey": "logout-key-0123456789", "confirm": True}
    )
    auth_relogin(app, {"operationKey": "relogin-key-0123456789"})

    assert port.calls == [
        (
            "logout",
            (CONTEXT,),
            {
                "operation_key": "logout-key-0123456789",
                "confirm": True,
                "expected_epoch": None,
            },
        ),
        (
            "relogin",
            (CONTEXT,),
            {"operation_key": "relogin-key-0123456789"},
        ),
    ]
    assert app.origins == [None, None]


def test_an_unassembled_logout_or_relogin_port_is_typed_unavailable():
    class BarePort:
        def status(self, context):
            return {"ok": True}

    app = FakeApp(BarePort())

    with pytest.raises(DaemonRequestError) as logout_refused:
        auth_logout(app, {"operationKey": "logout-key-0123456789", "confirm": True})
    assert logout_refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert logout_refused.value.code in {
        error.code for error in ONBOARDING_SPECS[6].errors
    }

    with pytest.raises(DaemonRequestError) as relogin_refused:
        auth_relogin(app, {"operationKey": "relogin-key-0123456789"})
    assert relogin_refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert relogin_refused.value.code in {
        error.code for error in ONBOARDING_SPECS[7].errors
    }


def test_declared_operation_conflict_reaches_the_wire_on_device_and_initialize():
    for handler, params, port_method in (
        (onboarding_device_ensure, {"operationKey": "device-key"}, "ensure_device"),
        (
            onboarding_initialize,
            {"operationKey": "initialize-key"},
            "initialize",
        ),
    ):
        port = FakePort()
        port.behavior[port_method] = OnboardingGatewayError(
            AUTH_OPERATION_CONFLICT, "another operation owns this key"
        )
        app = FakeApp(port)
        with pytest.raises(DaemonRequestError) as refused:
            handler(app, params)
        assert refused.value.code == AUTH_OPERATION_CONFLICT
        assert app.logs == []
