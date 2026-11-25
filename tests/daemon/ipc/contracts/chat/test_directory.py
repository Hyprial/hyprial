"""Chat directory declarations retain deliverability and trusted scope."""

from importlib import import_module
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import project_http_params_schema, validate_method_example_set


def test_targets_preserves_hosted_ipc_scope_and_forbids_browser_identity() -> None:
    bindings = import_module("hyprial.daemon.impl.request_catalog.message.directory").CHAT_DIRECTORY_BINDINGS
    registry = MethodRegistry.from_bindings(bindings)
    calls: list[dict[str, object]] = []
    rows = [{"targetKind": "agent", "targetUri": "agent:alice:node:worker",
             "actor": "agent:alice:node:worker", "status": "online", "deliverable": True}]
    app = SimpleNamespace(_ipc_targets=lambda params: calls.append(params) or {"targets": rows})
    assert registry.invoke(app, "targets", {"kind": "agent"}) == {"targets": rows}
    assert registry.invoke(app, "targets", {"kind": None}) == {"targets": rows}
    assert registry.invoke(app, "targets", {
        "actor": "agent:guest:node:visitor", "sessionRef": "visitor-session",
    }) == {"targets": rows}
    assert calls == [{"kind": "agent"}, {"kind": None}, {
        "actor": "agent:guest:node:visitor", "sessionRef": "visitor-session",
    }]
    spec = registry.get("targets").spec
    assert spec.http_exposed is True
    assert spec.mutates is False
    assert spec.retry == "safe"
    assert spec.audience == ("local-operator",)
    assert spec.params_schema["properties"]["actor"]["x-hyprial-http-forbidden"] is True
    assert spec.params_schema["properties"]["sessionRef"]["x-hyprial-http-forbidden"] is True
    assert registry.invoke(app, "targets", {
        "kind": "agent", "principal": "forged",
    }) == {"targets": rows}
    assert calls[-1] == {"kind": "agent", "principal": "forged"}
    projected = project_http_params_schema(
        spec.params_schema, spec.http_binding.params_constraints
    )
    assert list(Draft202012Validator(projected).iter_errors({
        "kind": "agent", "principal": "forged",
    }))


def test_targets_examples_validate_against_directory_spec() -> None:
    directory = import_module("hyprial.daemon.impl.request_catalog.message.directory")
    validate_method_example_set(directory.TARGETS_SPEC, directory.TARGETS_EXAMPLES)
