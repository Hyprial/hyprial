"""Typed, same-schema examples for daemon request method declarations.

Decision record (M-03, 2026-10-31)
- An example set is a sibling declaration keyed by method name.  It never
  repeats a params, result or error schema: every case is validated against the
  owning ``MethodSpec`` schemas before any OpenAPI projection or mock can use it.
- The scenario vocabulary is closed: ``success``, ``empty``, ``unconfigured``,
  ``invalid-params``, ``semantic-param-error`` and ``domain-error``.  Empty and
  unconfigured cases are legal successes; each states why that result is legal.
  A method without such a result states ``empty_not_applicable`` instead of
  inventing a payload.
- The validation boundary is declared, not inferred from an error code.  An
  ``invalid-params`` case is a true pre-dispatch negative: its params must fail
  the params schema and it must use a declared parameter error code.  A
  ``semantic-param-error`` case has schema-valid params and a declared
  handler-level rejection; legacy handlers keep authority over coercions,
  durations and requiredness, so ``INVALID_ARGUMENT`` is legitimate here.  A
  ``domain-error`` case also has schema-valid params and may use any declared
  code, including ``INVALID_ARGUMENT``.  The kind states where the rejection
  happens; the code does not.
- The true schema negative is never removed: every set needs an
  ``invalid-params`` case even when the handler also rejects some schema-valid
  inputs.  Its ``params`` may be any JSON value, including a non-object, because
  some legacy declarations accept every object and only a non-object is
  structurally invalid; requiring an object here would make those methods
  impossible to cover without tightening their declarations.  A method with no
  non-schema error can use ``domain_errors_not_applicable`` instead of
  inventing one.
- The projection is deliberately transport-neutral: examples carry business
  params/results/errors only.  The HTTP envelope belongs to the transport
  projection and is applied by the Contract Mock, not stored here.
- Declaration data is frozen and pure: these functions never read a home,
  identity, socket, credential store or clock.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from ..ipc_errors import INVALID_ARGUMENT, INVALID_PARAMS, INVALID_REQUEST

if TYPE_CHECKING:
    from ..method_spec import MethodSpec

__all__ = [
    "ExampleSpecError",
    "ExampleKind",
    "MethodExample",
    "MethodExampleError",
    "MethodExampleSet",
    "PARAM_ERROR_CODES",
    "example_to_json",
    "validate_method_example_set",
    "validate_method_example_sets",
]

ExampleKind = Literal[
    "success",
    "empty",
    "unconfigured",
    "invalid-params",
    "semantic-param-error",
    "domain-error",
]

EXAMPLE_KINDS = frozenset(
    {
        "success",
        "empty",
        "unconfigured",
        "invalid-params",
        "semantic-param-error",
        "domain-error",
    }
)
PARAM_ERROR_CODES = frozenset({INVALID_ARGUMENT, INVALID_PARAMS, INVALID_REQUEST})
_MAX_MESSAGE = 512


class ExampleSpecError(ValueError):
    """An example set cannot be consumed by the exporter or the mock."""


def _freeze(value: Any, where: str) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExampleSpecError(f"{where}: non-finite JSON number is not allowed")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExampleSpecError(f"{where}: JSON keys must be strings")
            frozen[key] = _freeze(item, f"{where}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{where}[{index}]") for index, item in enumerate(value))
    raise ExampleSpecError(
        f"{where}: {type(value).__name__} is not JSON-serialisable example data"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _validator(schema: Mapping[str, Any], where: str) -> Draft202012Validator:
    plain = _plain(schema)
    try:
        Draft202012Validator.check_schema(plain)
    except SchemaError as error:
        raise ExampleSpecError(f"{where}: {error.message}") from error
    return Draft202012Validator(plain, format_checker=FormatChecker())


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExampleSpecError(f"{where}: must be a non-empty string")
    return value


def _error_message(value: Any, where: str) -> str:
    text = _require_text(value, where)
    if len(text) > _MAX_MESSAGE:
        raise ExampleSpecError(f"{where}: must be at most {_MAX_MESSAGE} characters")
    if "\r" in text or "\n" in text:
        raise ExampleSpecError(f"{where}: must be a single line")
    return text


@dataclass(frozen=True, slots=True)
class MethodExampleError:
    """One business error instance used by an example case."""

    code: str
    message: str
    data: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_text(self.code, "code"))
        object.__setattr__(self, "message", _error_message(self.message, "message"))
        if self.data is not None:
            if not isinstance(self.data, Mapping):
                raise ExampleSpecError("error data must be an object")
            object.__setattr__(self, "data", _freeze(self.data, "error.data"))


@dataclass(frozen=True, slots=True)
class MethodExample:
    """One labelled scenario for one declared method."""

    name: str
    kind: ExampleKind
    description: str
    params: Any
    result: Mapping[str, Any] | None = None
    error: MethodExampleError | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_text(self.name, "example name"))
        object.__setattr__(
            self, "description", _require_text(self.description, "description")
        )
        object.__setattr__(self, "params", _freeze(self.params, "params"))
        if self.result is not None:
            if not isinstance(self.result, Mapping):
                raise ExampleSpecError("example result must be an object")
            object.__setattr__(self, "result", _freeze(self.result, "result"))
        if self.reason:
            object.__setattr__(self, "reason", _require_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class MethodExampleSet:
    """All scenarios for one method, plus explicit coverage exemptions."""

    method: str
    cases: tuple[MethodExample, ...]
    empty_not_applicable: str = ""
    domain_errors_not_applicable: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", _require_text(self.method, "method"))
        object.__setattr__(self, "cases", tuple(self.cases))
        if self.empty_not_applicable:
            object.__setattr__(
                self,
                "empty_not_applicable",
                _require_text(self.empty_not_applicable, "empty_not_applicable"),
            )
        if self.domain_errors_not_applicable:
            object.__setattr__(
                self,
                "domain_errors_not_applicable",
                _require_text(
                    self.domain_errors_not_applicable, "domain_errors_not_applicable"
                ),
            )


def _check_error(
    case: MethodExample,
    declared: Mapping[str, Any],
    data_validator: Draft202012Validator | None,
    *,
    kind: str,
) -> None:
    error = case.error
    assert error is not None
    if error.code not in declared:
        raise ExampleSpecError(
            f"{case.name}: {kind} error {error.code!r} is not declared by the method"
        )
    data_schema = declared[error.code]
    if data_schema is None:
        if error.data is not None:
            raise ExampleSpecError(
                f"{case.name}: declared error {error.code!r} has no data schema"
            )
        return
    if error.data is None:
        raise ExampleSpecError(
            f"{case.name}: declared error {error.code!r} requires error data"
        )
    if data_validator is None:
        raise ExampleSpecError(f"{case.name}: error data validator is missing")
    if not data_validator.is_valid(_plain(error.data)):
        raise ExampleSpecError(
            f"{case.name}: error data does not match the declared {error.code} schema"
        )


def validate_method_example_set(
    spec: "MethodSpec", example_set: MethodExampleSet
) -> tuple[MethodExample, ...]:
    """Validate one set against the owning declaration's own schemas."""

    if not isinstance(example_set, MethodExampleSet):
        raise ExampleSpecError("example set must be a MethodExampleSet")
    if example_set.method != spec.name:
        raise ExampleSpecError(
            f"{example_set.method}: set does not belong to declared method {spec.name}"
        )
    if not example_set.cases:
        raise ExampleSpecError(f"{spec.name}: example set is empty")

    params_validator = _validator(spec.params_schema, f"{spec.name}.params")
    result_validator = _validator(spec.result_schema, f"{spec.name}.result")
    declared_errors: dict[str, Mapping[str, Any] | None] = {
        error.code: error.data_schema for error in spec.errors
    }
    data_validators = {
        code: _validator(schema, f"{spec.name}.{code}.data") if schema is not None else None
        for code, schema in declared_errors.items()
    }

    names: set[str] = set()
    for case in example_set.cases:
        if case.name in names:
            raise ExampleSpecError(f"{spec.name}: duplicate example name {case.name!r}")
        names.add(case.name)
        if case.kind not in EXAMPLE_KINDS:
            raise ExampleSpecError(
                f"{case.name}: unknown example kind {case.kind!r}"
            )
        if case.kind == "invalid-params":
            if case.result is not None:
                raise ExampleSpecError(f"{case.name}: invalid-params cannot carry a result")
            if case.error is None:
                raise ExampleSpecError(f"{case.name}: invalid-params needs an error")
            if params_validator.is_valid(_plain(case.params)):
                raise ExampleSpecError(
                    f"{case.name}: invalid-params must fail the declared params schema"
                )
            if case.error.code not in PARAM_ERROR_CODES:
                raise ExampleSpecError(
                    f"{case.name}: invalid-params must use one of "
                    f"{sorted(PARAM_ERROR_CODES)}, got {case.error.code!r}"
                )
            _check_error(
                case,
                declared_errors,
                data_validators.get(case.error.code),
                kind="invalid-params",
            )
            continue
        if case.kind in ("semantic-param-error", "domain-error"):
            if case.result is not None:
                raise ExampleSpecError(f"{case.name}: {case.kind} cannot carry a result")
            if case.error is None:
                raise ExampleSpecError(f"{case.name}: {case.kind} needs an error")
            if not isinstance(case.params, Mapping):
                raise ExampleSpecError(
                    f"{case.name}: {case.kind} params must be a JSON object"
                )
            if not params_validator.is_valid(_plain(case.params)):
                raise ExampleSpecError(
                    f"{case.name}: {case.kind} params must pass the declared schema; "
                    "a schema-invalid case belongs to invalid-params"
                )
            _check_error(
                case,
                declared_errors,
                data_validators.get(case.error.code),
                kind=case.kind,
            )
            continue
        if case.error is not None:
            raise ExampleSpecError(f"{case.name}: {case.kind} cannot carry an error")
        if case.result is None:
            raise ExampleSpecError(f"{case.name}: {case.kind} needs a result")
        if not isinstance(case.params, Mapping):
            raise ExampleSpecError(
                f"{case.name}: {case.kind} params must be a JSON object"
            )
        if not params_validator.is_valid(_plain(case.params)):
            raise ExampleSpecError(
                f"{case.name}: example params must pass the declared schema"
            )
        if not result_validator.is_valid(_plain(case.result)):
            raise ExampleSpecError(
                f"{case.name}: example result must pass the declared schema"
            )
        if case.kind in ("empty", "unconfigured"):
            if not case.reason:
                raise ExampleSpecError(
                    f"{case.name}: {case.kind} must state why the empty/absent "
                    "result is legal"
                )
        elif case.reason:
            raise ExampleSpecError(
                f"{case.name}: reason is only valid for empty/unconfigured"
            )

    empty_cases = [case for case in example_set.cases if case.kind in ("empty", "unconfigured")]
    domain_cases = [
        case
        for case in example_set.cases
        if case.kind in ("domain-error", "semantic-param-error")
    ]
    if not any(case.kind == "success" for case in example_set.cases):
        raise ExampleSpecError(f"{spec.name}: at least one success example is required")
    if empty_cases and example_set.empty_not_applicable:
        raise ExampleSpecError(
            f"{spec.name}: empty_not_applicable contradicts the empty/unconfigured cases"
        )
    if not empty_cases and not example_set.empty_not_applicable:
        raise ExampleSpecError(
            f"{spec.name}: needs an empty/unconfigured example or an "
            "empty_not_applicable reason"
        )
    if not any(case.kind == "invalid-params" for case in example_set.cases):
        raise ExampleSpecError(f"{spec.name}: at least one invalid-params example is required")
    if domain_cases and example_set.domain_errors_not_applicable:
        raise ExampleSpecError(
            f"{spec.name}: domain_errors_not_applicable contradicts domain-error cases"
        )
    if not domain_cases and not example_set.domain_errors_not_applicable:
        raise ExampleSpecError(
            f"{spec.name}: needs a domain-error or semantic-param-error example "
            "or a domain_errors_not_applicable reason"
        )
    return example_set.cases


def validate_method_example_sets(
    specs: Iterable["MethodSpec"], example_sets: Iterable[MethodExampleSet]
) -> tuple[MethodExampleSet, ...]:
    """Validate a complete example catalog against a complete method catalog."""

    from ..method_spec import validate_method_specs

    catalog = validate_method_specs(specs)
    by_name = {spec.name: spec for spec in catalog}
    seen: set[str] = set()
    validated: list[MethodExampleSet] = []
    for example_set in example_sets:
        if not isinstance(example_set, MethodExampleSet):
            raise ExampleSpecError("example catalog entry must be a MethodExampleSet")
        if example_set.method in seen:
            raise ExampleSpecError(
                f"duplicate example set for {example_set.method!r}"
            )
        seen.add(example_set.method)
        spec = by_name.get(example_set.method)
        if spec is None:
            raise ExampleSpecError(
                f"{example_set.method}: example set names an unknown declared method"
            )
        validate_method_example_set(spec, example_set)
        validated.append(example_set)
    return tuple(sorted(validated, key=lambda item: item.method))


def example_to_json(example: MethodExample) -> dict[str, Any]:
    """Return one stable, JSON-safe projection of a validated example."""

    document: dict[str, Any] = {
        "name": example.name,
        "kind": example.kind,
        "description": example.description,
        "params": _plain(example.params),
    }
    if example.result is not None:
        document["result"] = _plain(example.result)
    if example.error is not None:
        error: dict[str, Any] = {
            "code": example.error.code,
            "message": example.error.message,
        }
        if example.error.data is not None:
            error["data"] = _plain(example.error.data)
        document["error"] = error
    if example.reason:
        document["reason"] = example.reason
    return document
