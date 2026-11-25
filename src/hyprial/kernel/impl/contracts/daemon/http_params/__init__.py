"""One pure projection for HTTP bodies, OpenAPI and listener validation.

Transport constraints can only intersect declared properties and add required
fields. The original MethodSpec params remain the IPC validation authority.
"""

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _contains_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool({"$ref", "$dynamicRef", "$recursiveRef"} & value.keys()) or any(
            _contains_reference(item) for item in value.values()
        )
    return isinstance(value, (list, tuple)) and any(_contains_reference(item) for item in value)


def _adds_validation(original: Any, addition: Any) -> bool:
    """Reject obvious no-ops, not decide arbitrary JSON Schema implication.

    Annotations do not constrain values. Known formats use the same default
    FormatChecker profile as HTTP and Contract Mock; unknown formats are
    annotations. Intersecting below guarantees non-widening, while this guard
    catches empty, annotation-only and plainly repeated rules.
    """
    if original is False or addition is True:
        return False
    if addition is False:
        return True
    previous = original if isinstance(original, Mapping) else {}
    for keyword, value in addition.items():
        if keyword not in Draft202012Validator.VALIDATORS:
            continue
        if keyword == "format" and value not in FormatChecker().checkers:
            continue
        if keyword in previous and value == previous[keyword]:
            continue
        if keyword == "allOf":
            if any(_adds_validation(original, part) for part in value):
                return True
            continue
        if keyword == "anyOf":
            if all(_adds_validation(original, part) for part in value):
                return True
            continue
        if keyword == "type":
            before = previous.get("type", ["null", "boolean", "object", "array", "number", "string"])
            before = {before} if isinstance(before, str) else set(before)
            after = {value} if isinstance(value, str) else set(value)
            if "number" in after:
                after.add("integer")
            if before <= after:
                continue
        if keyword in {"minLength", "minItems", "minProperties"} and value <= previous.get(keyword, 0):
            continue
        if keyword in {"minimum", "exclusiveMinimum"} and keyword in previous and value <= previous[keyword]:
            continue
        if keyword in {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}:
            if keyword in previous and value >= previous[keyword]:
                continue
        if keyword == "required" and set(value) <= set(previous.get(keyword, [])):
            continue
        if keyword == "properties":
            props = previous.get("properties", {})
            if not any(_adds_validation(props.get(name, True), item) for name, item in value.items()):
                continue
        if keyword in {"items", "additionalProperties", "unevaluatedItems", "unevaluatedProperties"}:
            if not _adds_validation(previous.get(keyword, True), value):
                continue
        if keyword == "uniqueItems" and value is False:
            continue
        return True
    return False


def project_http_params_schema(
    schema: Mapping[str, Any], constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove host-only fields and apply optional, non-widening intersections.

    No method names or identities are inferred here. ``constraints`` accepts
    only an object type, declared property constraints, and required names.
    It cannot replace the IPC schema or its additional-properties policy.
    Invalid transport declarations fail at assembly, before serving calls.
    """

    projected = _plain(schema)
    properties = projected.get("properties")
    if not isinstance(properties, dict):
        if constraints is not None:
            raise ValueError("HTTP constraints require declared properties")
        return projected
    forbidden = {
        name for name, declaration in properties.items()
        if isinstance(declaration, Mapping)
        and declaration.get("x-hyprial-http-forbidden") is True
    }
    for name in forbidden:
        properties.pop(name)
    if forbidden and isinstance(projected.get("required"), list):
        projected["required"] = [name for name in projected["required"] if name not in forbidden]
    if constraints is None:
        return projected
    if not isinstance(constraints, Mapping):
        raise ValueError("HTTP constraints must be an object schema")
    constraint = _plain(constraints)
    if constraint.get("type") != "object" or set(constraint) - {"type", "properties", "required"}:
        raise ValueError("HTTP constraints accept only object type, properties and required")
    if _contains_reference(constraint):
        raise ValueError("HTTP constraints must be inline; reference rebasing is unsupported")
    try:
        Draft202012Validator.check_schema(constraint)
    except SchemaError as error:
        raise ValueError("HTTP constraints are not a valid JSON schema") from error
    fields = constraint.get("properties", {})
    required = constraint.get("required", [])
    if set(fields) - set(properties) or set(required) - set(properties):
        raise ValueError("HTTP constraints cannot name undeclared or host-only parameters")
    original_required = projected.get("required", [])
    if not set(required) - set(original_required) and not any(
        _adds_validation(properties[name], addition) for name, addition in fields.items()
    ):
        raise ValueError("HTTP constraints must add validation, not only annotations or repeated rules")
    for name, addition in fields.items():
        properties[name] = {"allOf": [properties[name], addition]}
    if required:
        original = projected.get("required", [])
        projected["required"] = [*original, *(name for name in required if name not in original)]
    return projected
