"""The chat send declaration keeps legacy IPC and narrows only HTTP writes."""

from __future__ import annotations

from importlib import import_module
from threading import Lock
from types import MethodType, SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.daemon.impl.request_catalog.daemon.message_lookup import MESSAGE_SEND_LOOKUP_SPEC
from hyprial.daemon.impl.application.messaging.inbox_surface.send import _MessageSendMixin
from hyprial.daemon.impl.application.messaging.visibility import _VisibilityMixin
from hyprial.daemon.impl.application.messaging.delivery.sends import _require_deliverable_send_target
from hyprial.daemon.impl.alias_resolver import AliasKind, AliasStatus, AliasValidationError
from hyprial.kernel import DaemonRequestError, ipc_errors, validate_method_example_set
from hyprial.kernel.impl.contracts.daemon.http_params import project_http_params_schema


def _module():
    return import_module("hyprial.daemon.impl.request_catalog.message.send")


KEY_LOOKUP_VALID_KEY = "a" * 43


def _request() -> dict[str, object]:
    return {
        "to": ["agent:alice:node:worker"],
        "message": "hello",
        "summary": "hello",
        "conversationId": "conversation-1",
        "idempotencyKey": "frozen-operation-1-extended",
    }


def test_http_requires_a_nonblank_stable_key_without_changing_legacy_ipc() -> None:
    spec = _module().MESSAGE_SEND_SPEC
    http = spec.http_binding
    assert http is not None
    assert spec.mutates is True
    assert spec.retry == "never"
    assert spec.idempotency.mode == "never"
    assert spec.idempotency.key_field is None
    assert spec.idempotency.scope == "target-and-index"
    assert "idempotencyKey" in spec.params_schema["properties"]
    assert spec.http_exposed is True
    assert http.identity.mode == "local-owner-user-selector"
    assert http.identity.param == "actor"
    assert spec.params_schema["properties"]["sessionRef"]["x-hyprial-http-forbidden"] is True
    assert spec.params_schema["properties"]["from"]["x-hyprial-http-forbidden"] is True
    projected = project_http_params_schema(spec.params_schema, http.params_constraints)
    validator = Draft202012Validator(projected)
    assert not list(validator.iter_errors(_request()))
    assert list(validator.iter_errors({**_request(), "from": "user:forged"}))
    assert list(validator.iter_errors({**_request(), "sessionRef": "forged"}))
    assert list(validator.iter_errors({**_request(), "resourcePaths": []}))
    assert list(validator.iter_errors({**_request(), "providerMetadata": {}}))
    for key in (None, "", "   "):
        request = _request()
        if key is None:
            del request["idempotencyKey"]
        else:
            request["idempotencyKey"] = key
        assert list(validator.iter_errors(request))
    for field in ("to", "message", "summary"):
        request = _request()
        del request[field]
        assert list(validator.iter_errors(request))
    # G0: the conversation id is optional over HTTP, but never blank or null.
    without_id = _request()
    del without_id["conversationId"]
    assert not list(validator.iter_errors(without_id))
    assert list(validator.iter_errors({**_request(), "conversationId": None}))
    for value in ("", "   "):
        assert list(validator.iter_errors({**_request(), "conversationId": value}))
    assert not list(Draft202012Validator(spec.params_schema).iter_errors({
        "to": ["agent:alice:node:worker"], "message": "hello", "summary": "hello",
    }))


def test_binding_preserves_handler_authority_for_legacy_params() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    calls: list[tuple[str, dict[str, object], str | None]] = []
    result = {
        "ok": True, "actingPrincipal": "user:alice", "operationId": "frozen-operation-1-extended",
        "conversationId": "conversation-1", "messageId": "message-1",
        "deliveries": [{"target": "agent:alice:node:worker", "messageId": "message-1",
                        "accepted": True, "queued": False}],
    }
    app = SimpleNamespace(_ipc_message_send=lambda method, params, _trusted_message_origin=None:
                          calls.append((method, params, _trusted_message_origin)) or result)
    request = _request()
    assert registry.invoke(app, "message.send", request, trusted_message_origin="operator") == result
    legacy = {**{key: value for key, value in request.items() if key != "idempotencyKey"},
              "from": "user:alice"}
    assert registry.invoke(app, "message.send", legacy) == result
    assert calls == [("message.send", request, "operator"), ("message.send", legacy, None)]
    assert len(calls) == 2


def test_unknown_top_level_ipc_field_reaches_the_send_handler() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    calls: list[dict[str, object]] = []
    result = {
        "ok": True, "actingPrincipal": "user:alice", "operationId": "operation-1",
        "conversationId": "conversation-1", "messageId": "message-1",
        "deliveries": [{
            "target": "agent:alice:node:worker", "messageId": "message-1",
            "accepted": True, "queued": False,
        }],
    }
    app = SimpleNamespace(
        _ipc_message_send=lambda _method, raw, _trusted_message_origin=None:
        calls.append(raw) or result
    )
    params = {**_request(), "legacyExtension": {"kept": True}}
    assert registry.invoke(app, "message.send", params) == result
    assert calls == [params]
    spec = registry.get("message.send").spec
    projected = project_http_params_schema(
        spec.params_schema, spec.http_binding.params_constraints
    )
    assert list(Draft202012Validator(projected).iter_errors(params))


def test_examples_follow_the_same_spec() -> None:
    module = _module()
    validate_method_example_set(module.MESSAGE_SEND_SPEC, module.MESSAGE_SEND_EXAMPLES)




def test_real_early_handler_errors_keep_legacy_order_and_data() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    app = SimpleNamespace(
        _authenticated_message_caller=lambda *args, **kwargs: SimpleNamespace(
            sender="user:alice", subject="user:alice", via="operator",
        ),
        _queue_agent_activity=lambda *args: None,
        agents=None,
    )
    app._ipc_message_send = MethodType(_MessageSendMixin._ipc_message_send, app)
    for params, code, reason in (
        ({"summary": 123}, ipc_errors.SUMMARY_REQUIRED, "not_string"),
        ({"to": "bad"}, ipc_errors.SUMMARY_REQUIRED, "missing"),
        ({"summary": "x" * 513}, ipc_errors.SUMMARY_REQUIRED, None),
        ({"summary": "valid", "to": "bad"}, ipc_errors.INVALID_ARGUMENT, None),
    ):
        with pytest.raises(DaemonRequestError) as original:
            app._ipc_message_send("message.send", params)
        assert original.value.code == code
        if reason is not None:
            assert original.value.data["reason"] == reason
        with pytest.raises(DaemonRequestError) as registered:
            registry.invoke(app, "message.send", params)
        assert registered.value.code == original.value.code
        assert registered.value.data == original.value.data
        if params.get("summary") == "x" * 513:
            assert registered.value.data == {"summaryBytes": 513, "maxBytes": 512}


def test_authentication_error_precedes_missing_summary_for_legacy_ipc() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    app = SimpleNamespace()
    app._authenticated_message_caller = MethodType(
        _VisibilityMixin._authenticated_message_caller, app,
    )
    app._ipc_message_send = MethodType(_MessageSendMixin._ipc_message_send, app)
    for params in ({}, {"actor": 123}, {"actor": ""}):
        with pytest.raises(DaemonRequestError) as original:
            app._ipc_message_send("message.send", params)
        with pytest.raises(DaemonRequestError) as registered:
            registry.invoke(app, "message.send", params)
        assert registered.value.code == original.value.code == ipc_errors.INVALID_ARGUMENT
        assert registered.value.data == original.value.data


def test_real_target_helper_error_keeps_target_data() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    def refuse(method, params, _trusted_message_origin=None):
        _require_deliverable_send_target(params["to"][0])
    app = SimpleNamespace(_ipc_message_send=refuse)
    with pytest.raises(DaemonRequestError) as original:
        _require_deliverable_send_target("host:alice:node")
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(app, "message.send", {**_request(), "to": ["host:alice:node"]})
    assert registered.value.code == original.value.code
    assert registered.value.data == original.value.data


def test_alias_ambiguity_keeps_legacy_alias_data() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    alias = AliasValidationError(
        status=AliasStatus.AMBIGUOUS, kind=AliasKind.AGENT,
        alias="worker", candidates=("a.one", "a.two"),
    )
    def refuse(method, params, _trusted_message_origin=None):
        raise DaemonRequestError(alias.code, str(alias), alias.data)
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(SimpleNamespace(_ipc_message_send=refuse), "message.send", _request())
    assert registered.value.code == ipc_errors.AMBIGUOUS_TARGET
    assert registered.value.data == alias.data


def test_real_authentication_refusal_keeps_claimed_identity_data() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    claimed = "agent:alice:node:worker"
    def refuse(method, params, _trusted_message_origin=None):
        raise _VisibilityMixin._sender_not_authenticated(claimed)
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(SimpleNamespace(_ipc_message_send=refuse), "message.send", _request())
    assert registered.value.code == ipc_errors.SENDER_NOT_AUTHENTICATED
    assert registered.value.data == {"claimedIdentity": claimed}






def test_dispatch_diagnostic_overload_keeps_transient_error_code() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    def refuse(method, params, _trusted_message_origin=None):
        raise ipc_errors.DaemonUnavailableError("dispatch diagnostic authority is overloaded")
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(SimpleNamespace(_ipc_message_send=refuse), "message.send", _request())
    assert registered.value.code == ipc_errors.DAEMON_UNAVAILABLE


def _user_send_app(user_delivery) -> SimpleNamespace:
    app = SimpleNamespace(
        _authenticated_message_caller=lambda *args, **kwargs: SimpleNamespace(
            sender="user:alice", subject="user:alice", via="operator", on_behalf_of=None,
        ),
        _queue_agent_activity=lambda *args: None,
        agents=None,
        _resolve_and_validate_input_alias=lambda target, **kwargs: target,
        _link_reply_to_param=lambda value: value,
        _dispatch_diagnostics=None,
        _dispatch_without_pac_lock=Lock(),
        _dispatch_gate_conversations={},
        _user_delivery=user_delivery,
        _thread_bind=lambda *args, **kwargs: None,
    )
    app._ipc_message_send = MethodType(_MessageSendMixin._ipc_message_send, app)
    return app


@pytest.mark.parametrize("code", (
    # Release sync: the cell-model S10 receiver folds TARGET_SQUIRE_UNCONFIGURED
    # into TARGET_UNRESOLVED and an unregistered owner-thread adapter into
    # USER_DELIVERY_UNAVAILABLE.  Through the registry both must keep their
    # code and receipt identity instead of becoming DAEMON_ERROR.
    ipc_errors.TARGET_UNRESOLVED,
    ipc_errors.USER_DELIVERY_UNAVAILABLE,
))
def test_s10_user_delivery_refusal_survives_the_registry(code: str) -> None:
    from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryResult

    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    seen: list[object] = []

    class _Refusing:
        def deliver(self, request):
            seen.append(request)
            return UserDeliveryResult(
                message_id=request.message_id, accepted=False, code=code,
                message="the owner's squire is not configured",
            )

    app = _user_send_app(_Refusing())
    # replyTo skips the advisory open-request read (no inbox on this fake).
    params = {**_request(), "to": ["user:alice"], "replyTo": "request-1"}
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(app, "message.send", params)
    assert len(seen) == 1
    assert registered.value.code == code
    assert registered.value.data == {
        "target": "user:alice", "messageId": seen[0].message_id,
    }


def test_real_user_delivery_unavailable_keeps_its_code() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    app = SimpleNamespace(
        _authenticated_message_caller=lambda *args, **kwargs: SimpleNamespace(
            sender="user:alice", subject="user:alice", via="operator", on_behalf_of=None,
        ),
        _queue_agent_activity=lambda *args: None,
        agents=None,
        _resolve_and_validate_input_alias=lambda target, **kwargs: target,
        _link_reply_to_param=lambda value: value,
        _dispatch_diagnostics=None,
        _dispatch_without_pac_lock=Lock(),
        _dispatch_gate_conversations={},
        _user_delivery=None,
        # cell-model: every send binds its endpoint pair to a thread first.
        _thread_bind=lambda *args, **kwargs: None,
    )
    app._ipc_message_send = MethodType(_MessageSendMixin._ipc_message_send, app)
    params = {**_request(), "to": ["user:alice"], "replyTo": "request-1"}
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(app, "message.send", params)
    assert registered.value.code == ipc_errors.USER_DELIVERY_UNAVAILABLE
    assert set(registered.value.data) == {"target", "messageId"}
    assert registered.value.data["target"] == "user:alice"


def test_a_user_agent_cell_push_refusal_survives_the_registry(tmp_path) -> None:
    """I7a: the receiver's refusal when no user agent cell target was sent.

    ``push_owner_copy`` folds all not-sent targets into a refusal with the
    first code; message.send re-raises it with the per-target
    ``{target, messageId}`` and the registry keeps it instead of rewriting
    it to ``DAEMON_ERROR``.
    """

    from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryResult
    from hyprial.daemon.impl.adapters.user_delivery.receiver import (
        ReceiverUserDelivery, UserDeliveryLedger,
    )
    receiver = ReceiverUserDelivery(
        node_id="node", owns=lambda owner: True,
        ledger=UserDeliveryLedger(tmp_path / "user-deliveries.json"),
        owner_push=lambda request: UserDeliveryResult(
            message_id=request.message_id, accepted=False,
            code=ipc_errors.USER_DELIVERY_FAILED,
            message="no copy was pushed to the user agent cell's chats",
            definitely_not_sent=True,
        ),
    )
    seen: list[object] = []

    def deliver(request):
        seen.append(request)
        return receiver.handle(request)

    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    app = _user_send_app(SimpleNamespace(deliver=deliver))
    params = {**_request(), "to": ["user:alice"], "replyTo": "request-1"}
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(app, "message.send", params)
    assert len(seen) == 1
    assert registered.value.code == ipc_errors.USER_DELIVERY_FAILED
    assert registered.value.data == {
        "target": "user:alice", "messageId": seen[0].message_id,
    }
    assert receiver.ledger.get(seen[0].idempotency_key) is None


def test_send_receipts_keep_user_push_resource_and_unknown_fields() -> None:
    schema = _module().MESSAGE_SEND_SPEC.result_schema
    validator = Draft202012Validator(schema)
    base = {
        "ok": False, "actingPrincipal": "user:alice", "operationId": "key-1",
        "conversationId": "conversation-1", "deliveries": [],
    }
    unknown = {
        "target": "user:alice", "messageId": "message-1", "accepted": False,
        "queued": False, "outcomeKnown": False, "code": "SUBMIT_OUTCOME_UNKNOWN",
        "pushed": False, "pushCode": "TARGET_UNRESOLVED",
    }
    pushed = {
        "target": "user:alice", "messageId": "message-2", "accepted": True,
        "queued": False, "pushed": True, "via": "cell",
        "resourceDeliveries": [{
            "kind": "file", "fileName": "report.txt", "mediaType": "text/plain",
            "sizeBytes": 3, "fileType": "stream", "fileKey": "file-1",
            "nativeMessageId": "om-file-1",
        }, {
            "kind": "image", "fileName": "pixel.png", "mediaType": "image/png",
            "sizeBytes": 3, "imageKey": "img-1", "nativeMessageId": "om-image-1",
        }],
    }
    for delivery in (unknown, pushed):
        assert not list(validator.iter_errors({**base, "deliveries": [delivery]}))


def test_route_error_codes_are_not_declared_on_message_send() -> None:
    declared = {error.code for error in _module().MESSAGE_SEND_SPEC.errors}
    assert not {
        "ROUTE_ADAPTER_UNCONFIGURED", "ROUTE_SEND_FAILED", "ROUTE_RESOURCES_UNSUPPORTED",
        "ROUTE_RESOURCE_REPLY_UNSUPPORTED", "ROUTE_NOT_CONFIGURED",
        "ROUTE_FANOUT_MEMBER_INVALID", "ROUTE_APP_SCOPE_REQUIRED", "ROUTE_APP_NOT_IN_CHAT",
        "ROUTE_APP_BOT_ABILITY_DISABLED", "ROUTE_OPERATOR_NOT_IN_CHAT",
    } & declared
    assert not {code for code in declared if code.startswith("ROUTE_")}


def test_resource_refusals_are_declared_on_the_owner_push_codes() -> None:
    errors = {error.code: error for error in _module().MESSAGE_SEND_SPEC.errors}
    assert {
        ipc_errors.UNSUPPORTED_TARGET, ipc_errors.INVALID_ARGUMENT,
        "RESOURCE_NOT_FOUND", "RESOURCE_TOO_LARGE", "RESOURCE_EMPTY",
        "RESOURCE_KIND_UNSUPPORTED", "RESOURCE_IMAGE_TYPE_UNSUPPORTED",
        ipc_errors.ATTACHMENT_PATH_REFUSED,
    } <= set(errors)
    assert "target" in errors[ipc_errors.UNSUPPORTED_TARGET].data_schema["properties"]


def test_a_refused_route_target_keeps_its_target_data_through_the_registry() -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)

    def refuse(method, params, _trusted_message_origin=None):
        raise DaemonRequestError(
            ipc_errors.UNSUPPORTED_TARGET, "retired", {"target": "route:feishu:group"}
        )

    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(
            SimpleNamespace(_ipc_message_send=refuse), "message.send",
            {**_request(), "to": ["route:feishu:group"]},
        )
    assert registered.value.code == ipc_errors.UNSUPPORTED_TARGET
    assert registered.value.data == {"target": "route:feishu:group"}


def test_identity_codes_are_not_declared_on_message_send() -> None:
    """R2: the user push no longer reaches the identity resolver."""

    declared = {error.code for error in _module().MESSAGE_SEND_SPEC.errors}
    assert not {
        ipc_errors.IDENTITY_CONFLICT,
        ipc_errors.IDENTITY_UNBOUND,
        ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
    } & declared


def test_retired_squire_codes_are_not_declared_on_message_send() -> None:
    declared = {error.code for error in _module().MESSAGE_SEND_SPEC.errors}
    assert not {code for code in declared if code.startswith("TARGET_SQUIRE_")}
    assert "PROXY_SEND_REFUSED" not in declared


@pytest.mark.parametrize("code", (
    "USER_DELIVERY_OVERLOADED",
    ipc_errors.USER_DELIVERY_FAILED,
    ipc_errors.TARGET_UNRESOLVED,
    ipc_errors.USER_DELIVERY_UNAVAILABLE,
))
def test_user_delivery_refusal_is_declared_with_receipt_identity(code: str) -> None:
    registry = MethodRegistry.from_bindings(_module().MESSAGE_SEND_BINDINGS)
    def overloaded(method, params, _trusted_message_origin=None):
        raise DaemonRequestError(
            code, "receiver delivery refused",
            {"target": params["to"][0], "messageId": "message-1"},
        )
    with pytest.raises(DaemonRequestError) as registered:
        registry.invoke(
            SimpleNamespace(_ipc_message_send=overloaded), "message.send",
            {**_request(), "to": ["user:alice"]},
        )
    assert registered.value.code == code
    assert registered.value.data == {"target": "user:alice", "messageId": "message-1"}


def test_frozen_http_send_identity_is_usable_for_lookup_after_lost_response() -> None:
    send = _module().MESSAGE_SEND_SPEC
    params = _request()
    projected = project_http_params_schema(send.params_schema, send.http_binding.params_constraints)
    assert not list(Draft202012Validator(projected).iter_errors(params))
    lookup = {
        "target": params["to"][0], "text": params["message"],
        "conversationId": params["conversationId"],
        "idempotencyKey": params["idempotencyKey"],
    }
    assert not list(Draft202012Validator(MESSAGE_SEND_LOOKUP_SPEC.params_schema).iter_errors(lookup))
    assert lookup["conversationId"] == params["conversationId"]
    assert lookup["idempotencyKey"] == params["idempotencyKey"]


def test_c1_http_send_rejects_multiple_targets_and_weak_operation_key() -> None:
    spec = _module().MESSAGE_SEND_SPEC
    projected = project_http_params_schema(spec.params_schema, spec.http_binding.params_constraints)
    validator = Draft202012Validator(projected)
    assert list(validator.iter_errors({**_request(), "to": [
        "agent:alice:node:worker", "agent:alice:node:other",
    ]}))
    assert list(validator.iter_errors({**_request(), "idempotencyKey": "short"}))
    for target in ("host:alice:node", "user:alice", "agent:alice: :worker"):
        assert list(validator.iter_errors({**_request(), "to": [target]}))


def test_c1_key_lookup_spec_is_isolated_read_only_and_key_only() -> None:
    module = _module()
    spec = module.MESSAGE_SEND_KEY_LOOKUP_SPEC
    assert spec.name == "message.send.key_lookup"
    assert spec.mutates is False
    assert spec.retry == "safe"
    assert spec.http_exposed is True
    assert spec.http_binding.identity.mode == "local-owner-user-selector"
    assert spec.http_binding.identity.param == "actor"
    projected = project_http_params_schema(spec.params_schema, spec.http_binding.params_constraints)
    validator = Draft202012Validator(projected)
    assert not list(validator.iter_errors({"operationKey": KEY_LOOKUP_VALID_KEY}))
    for extra in ("text", "target", "conversationId", "from", "sessionRef"):
        assert list(validator.iter_errors({"operationKey": KEY_LOOKUP_VALID_KEY, extra: "forged"}))
    assert list(validator.iter_errors({"operationKey": "short"}))
    validate_method_example_set(spec, module.MESSAGE_SEND_KEY_LOOKUP_EXAMPLES)


def test_c1_key_lookup_binding_preserves_authenticated_handler() -> None:
    module = _module()
    registry = MethodRegistry.from_bindings(module.MESSAGE_SEND_KEY_LOOKUP_BINDINGS)
    calls = []
    app = SimpleNamespace(
        _ipc_message_send_key_lookup=lambda method, params, _trusted_message_origin=None:
        calls.append((method, params, _trusted_message_origin)) or
        {"status": "unknown", "reason": "not-found"},
    )
    params = {"operationKey": KEY_LOOKUP_VALID_KEY, "legacyExtension": True}
    assert registry.invoke(app, "message.send.key_lookup", params,
                           trusted_message_origin="ipc") == {
        "status": "unknown", "reason": "not-found",
    }
    assert calls == [("message.send.key_lookup", params, "ipc")]
