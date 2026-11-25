"""Progress query declarations keep the daemon's non-consuming authority."""

from importlib import import_module
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import (
    DaemonRequestError, ipc_errors, project_http_params_schema,
    validate_method_example_set,
)


def test_progress_list_preserves_consumer_scope_and_result_shape() -> None:
    module = import_module("hyprial.daemon.impl.request_catalog.message.progress")
    registry = MethodRegistry.from_bindings(module.CHAT_PROGRESS_BINDINGS)
    params = {"actor": "user:alice", "deliveryId": "delivery-1", "sinceSeq": 0}
    result = {"ok": True, "events": [], "daemonEpoch": "epoch-1"}
    calls: list[tuple[str, dict[str, object], str | None]] = []

    def progress(method: str, raw: dict[str, object], trusted: str | None = None) -> dict[str, object]:
        calls.append((method, raw, trusted))
        return result

    app = SimpleNamespace(_ipc_progress_list=lambda method, raw, _trusted_message_origin=None:
                          progress(method, raw, _trusted_message_origin))
    assert registry.invoke(app, "progress.list", params, trusted_message_origin="ipc") == result
    assert calls == [("progress.list", params, "ipc")]
    binding = registry.get("progress.list")
    assert binding is not None
    assert binding.spec.http_exposed is True
    assert binding.spec.http_binding.identity.mode == "local-owner-user-selector"
    assert binding.spec.http_binding.identity.param == "actor"
    assert binding.spec.retry == "safe"


def test_progress_list_handler_owns_legacy_cursor_validation_and_http_is_strict() -> None:
    module = import_module("hyprial.daemon.impl.request_catalog.message.progress")
    registry = MethodRegistry.from_bindings(module.CHAT_PROGRESS_BINDINGS)
    params = {"actor": "user:alice", "sinceSeq": -1}
    calls: list[dict[str, object]] = []
    app = SimpleNamespace(
        _ipc_progress_list=lambda _method, raw, _trusted_message_origin=None:
        calls.append(raw) or {"ok": True, "events": [], "daemonEpoch": "epoch-1"}
    )
    assert registry.invoke(app, "progress.list", params) == {
        "ok": True, "events": [], "daemonEpoch": "epoch-1",
    }
    assert calls == [params]
    spec = registry.get("progress.list").spec
    projected = project_http_params_schema(
        spec.params_schema, spec.http_binding.params_constraints
    )
    assert list(Draft202012Validator(projected).iter_errors(params))


def test_progress_list_preserves_declared_sender_error_data() -> None:
    module = import_module("hyprial.daemon.impl.request_catalog.message.progress")
    registry = MethodRegistry.from_bindings(module.CHAT_PROGRESS_BINDINGS)

    def unresolved(*_args: object, **_kwargs: object) -> None:
        raise DaemonRequestError(ipc_errors.SENDER_UNRESOLVED, "unknown sender", {"sender": "missing"})

    app = SimpleNamespace(_ipc_progress_list=unresolved)
    with pytest.raises(DaemonRequestError) as caught:
        registry.invoke(app, "progress.list", {"actor": "missing"})
    assert caught.value.code == ipc_errors.SENDER_UNRESOLVED
    assert caught.value.data == {"sender": "missing"}


def test_progress_examples_match_spec() -> None:
    module = import_module("hyprial.daemon.impl.request_catalog.message.progress")
    validate_method_example_set(module.PROGRESS_LIST_SPEC, module.PROGRESS_LIST_EXAMPLES)
