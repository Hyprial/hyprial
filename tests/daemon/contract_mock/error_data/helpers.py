"""Synthetic declaration with a genuinely optional error payload."""

from hyprial.kernel import (
    HttpBinding, IdentityBinding, MethodError, MethodExample, MethodExampleError,
    MethodExampleSet, MethodSpec,
)

DATA_SCHEMA = {
    "type": "object", "properties": {"field": {"type": "string"}},
    "additionalProperties": False,
}


def declaration(schema=DATA_SCHEMA):
    return MethodSpec(
        name="probe.error", owner="daemon.probe",
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"], "additionalProperties": False},
        errors=(MethodError("INVALID_ARGUMENT", schema),), http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")),
    )


def examples(data):
    error = MethodExampleError("INVALID_ARGUMENT", "synthetic refusal", data)
    return MethodExampleSet(
        method="probe.error", empty_not_applicable="a probe has no empty result",
        cases=(
            MethodExample(name="ok", kind="success", description="probe succeeds",
                          params={}, result={"ok": True}),
            MethodExample(name="bad-input", kind="invalid-params", description="unknown key",
                          params={"extra": 1}, error=error),
            MethodExample(name="refused", kind="domain-error", description="probe refuses",
                          params={}, error=error),
        ),
    )
