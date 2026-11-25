"""``agent.attach`` / ``agent.detach`` are complete MethodSpec declarations.

Both are declared by exact name (the ``agent`` family stays open), bound to
real handlers, local-operator only, mutating and idempotent by agent name
(declared ``retry: never``: no blind replay of a write), with closed params
and results, catalogued but not HTTP exposed: the HTTP gateway serves no
mutating method until write admission (H-03) is integrated.
"""

from __future__ import annotations

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_FAMILIES, daemon_openapi
from hyprial.kernel import ipc_errors, validate_method_spec

ATTACH_ERRORS = {
    ipc_errors.INVALID_ARGUMENT,
    ipc_errors.AGENT_EXISTS,
    "AGENT_VERSION_CONFLICT",
    "AGENT_ERROR",
    ipc_errors.USER_AGENT_IMMUTABLE,
}
DETACH_ERRORS = {
    ipc_errors.INVALID_ARGUMENT,
    ipc_errors.AGENT_NOT_FOUND,
    "AGENT_VERSION_CONFLICT",
    "AGENT_ERROR",
    ipc_errors.USER_AGENT_IMMUTABLE,
}


def test_both_methods_are_declared_and_bound() -> None:
    assert "agent" not in MIGRATED_FAMILIES
    for name, errors in (("agent.attach", ATTACH_ERRORS), ("agent.detach", DETACH_ERRORS)):
        binding = METHOD_REGISTRY.get(name)
        assert binding is not None, name
        spec = binding.spec
        validate_method_spec(spec)
        assert spec.owner == "daemon.agents"
        assert spec.mutates is True and spec.mode == "unary"
        assert spec.retry == "never"
        assert spec.idempotency is not None and spec.idempotency.key_field is None
        assert spec.idempotency.scope == name.replace(".", "-") + "-by-name"
        assert spec.audience == ("local-operator",)
        assert spec.http_exposed is False and spec.http_binding is None
        assert spec.confirmation == "none"
        assert {error.code for error in spec.errors} == errors
        assert spec.params_schema["additionalProperties"] is False
        assert spec.params_schema["required"] == ("name",)
        assert spec.result_schema["additionalProperties"] is False
        assert callable(binding.handler)


def test_the_contract_lists_them_without_an_http_path() -> None:
    document = daemon_openapi()
    names = {entry["name"] for entry in document["x-hyprial-methods"]}
    assert {"agent.attach", "agent.detach"} <= names
    assert "/daemon/request/agent.attach" not in document["paths"]
    assert "/daemon/request/agent.detach" not in document["paths"]
