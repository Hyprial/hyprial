"""Catalog migration must retain OrgFS's published lowercase error codes."""

import pytest

from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, MethodSpecError, validate_method_spec


def spec(code):
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    return MethodSpec(name="orgfs.probe", owner="daemon.orgfs", params_schema=empty,
                      result_schema=empty, errors=(MethodError(code),),
                      audience=("local-operator",), idempotency=IdempotencySpec(mode="safe", scope="read"))


@pytest.mark.parametrize("code", ["unknown-space", "unknown-doc", "stale-write", "content-pending"])
def test_legacy_lowercase_wire_errors_remain_declarable(code):
    validate_method_spec(spec(code))


@pytest.mark.parametrize("code", ["bad.code", "bad code", "-bad", "bad-", "bad--code", "Bad-Code"])
def test_malformed_error_codes_still_refused(code):
    with pytest.raises(MethodSpecError):
        validate_method_spec(spec(code))
