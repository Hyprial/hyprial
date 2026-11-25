"""Declaration self-checks moved off the import path stay enforced.

Decision: assembling the registry (``freeze``) no longer runs the Draft
2020-12 meta-schema verdict or builds validators; ``verify()`` does, and the
production catalog is verified here and in ``check_request_contracts``.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MethodRegistry,
    verify_registry,
)
from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    MethodSpecError,
    export_openapi,
    ipc_errors,
    validate_method_spec,
)
from hyprial.kernel.impl.contracts.daemon.examples import (
    ExampleSpecError,
    MethodExample,
    MethodExampleSet,
)

BAD_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "not-a-json-schema-type"}},
    "required": [],
    "additionalProperties": False,
}


def _spec(**overrides: Any) -> MethodSpec:
    values: dict[str, Any] = {
        "name": "probe.read",
        "owner": "daemon.probe",
        "params_schema": {
            "type": "object",
            "properties": {"from": {"type": "string", "minLength": 1}},
            "required": ["from"],
            "additionalProperties": False,
        },
        "result_schema": {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "errors": (MethodError(ipc_errors.INVALID_ARGUMENT),),
        "audience": ("local-operator",),
        "idempotency": IdempotencySpec(mode="safe", scope="read-only-query"),
    }
    values.update(overrides)
    return MethodSpec(**values)


def _handler(_app: Any, _params: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"ok": True}


def test_the_production_catalog_passes_the_full_self_check() -> None:
    verify_registry()
    METHOD_REGISTRY.verify()


@pytest.mark.parametrize("field", ["params_schema", "result_schema"])
def test_an_invalid_schema_is_rejected_by_verify_not_by_assembly(field: str) -> None:
    spec = _spec(**{field: BAD_SCHEMA})
    registry = MethodRegistry.from_bindings(((spec, _handler),))
    with pytest.raises(MethodSpecError, match=field):
        registry.verify()
    with pytest.raises(MethodSpecError, match=field):
        validate_method_spec(spec)


def test_an_invalid_error_data_schema_is_rejected_by_verify() -> None:
    spec = _spec(errors=(MethodError(ipc_errors.INVALID_ARGUMENT, BAD_SCHEMA),))
    registry = MethodRegistry.from_bindings(((spec, _handler),))
    with pytest.raises(MethodSpecError, match="errors"):
        registry.verify()


def test_structural_checks_still_fail_at_assembly() -> None:
    registry = MethodRegistry()
    registry.bind(_spec(), _handler)
    with pytest.raises(ValueError, match="duplicate"):
        registry.bind(_spec(), _handler)
    with pytest.raises(MethodSpecError, match="invalid owner"):
        MethodRegistry.from_bindings(((_spec(owner="Not An Owner"), _handler),))


def test_an_invalid_example_is_rejected_by_the_openapi_projection() -> None:
    spec = _spec()
    bad = MethodExampleSet(
        "probe.read",
        (
            MethodExample(
                "wrong-result", "success", "result violates the declaration",
                {"from": "agent:a:b:c"}, result={"ok": False},
            ),
        ),
        empty_not_applicable="probe",
        domain_errors_not_applicable="probe",
    )
    with pytest.raises(ExampleSpecError):
        export_openapi((spec,), example_sets=(bad,))


def test_validators_are_lazy_cached_and_validation_is_unchanged() -> None:
    registry = MethodRegistry.from_bindings(((_spec(), _handler),))
    assert registry._params == {} and registry._results == {}
    assert registry.invoke(object(), "probe.read", {"from": "x"}) == {"ok": True}
    first = registry._params_validator("probe.read")
    assert registry._params_validator("probe.read") is first
    with pytest.raises(DaemonRequestError) as refused:
        registry.invoke(object(), "probe.read", {"from": ""})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "probe.read params do not match the declared schema at from" in str(refused.value)


def test_concurrent_first_use_builds_one_validator_per_method() -> None:
    registry = MethodRegistry.from_bindings(((_spec(), _handler),))
    barrier = threading.Barrier(8)
    seen: list[object] = []

    def worker() -> None:
        barrier.wait()
        registry.invoke(object(), "probe.read", {"from": "x"})
        seen.append(registry._params_validator("probe.read"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 8 and len({id(item) for item in seen}) == 1
