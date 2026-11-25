"""Generated contract consumers inherit HTTP-only narrowing.

The served mock is JavaScript; its refusal of a missing key and of an
unsupported ``format`` keyword is tested in gui/tests/contract-mock.
"""

from dataclasses import replace

import pytest

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, MethodSpecError, export_openapi
from tests.daemon.ipc.contracts.http_params.test_constraints import declaration
from tests.daemon.ipc.contracts.http_params.test_formats import VALID_UUID, uuid_declaration


def examples(params):
    return (MethodExampleSet(
        method="probe.submit", empty_not_applicable="a write returns one result",
        domain_errors_not_applicable="synthetic write has no domain condition",
        cases=(
            MethodExample(name="accepted", kind="success", description="synthetic accepted write",
                          params=params, result={"ok": True}),
            MethodExample(name="bad-body", kind="invalid-params", description="object required",
                          params=[], error=MethodExampleError("INVALID_ARGUMENT", "object required")),
        ),
    ),)


def test_ipc_valid_success_example_cannot_be_exported_as_invalid_http_success():
    spec = declaration()
    old = replace(spec, http_binding=replace(spec.http_binding, params_constraints=None))
    assert export_openapi((old,), example_sets=examples({}))
    with pytest.raises(MethodSpecError, match="example violates HTTP constraints"):
        export_openapi((spec,), example_sets=examples({}))


def test_http_success_example_must_satisfy_format_not_only_required_key():
    # Required makes this declaration pass the old no-op guard, isolating the
    # exporter's missing FormatChecker from the separate projection regression.
    spec = uuid_declaration(require_key=True)
    with pytest.raises(MethodSpecError, match="example violates HTTP constraints"):
        export_openapi((spec,), example_sets=examples({"key": "not-a-uuid"}))
    assert export_openapi((spec,), example_sets=examples({"key": VALID_UUID}))


