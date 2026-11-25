"""Load a business OpenAPI projection as the Contract Mock's only method source.

Decision record (M-03, 2026-10-31)
- The mock reads the same ``contract/daemon-request/openapi.json`` projection
  that MethodSpec exports; it does not import the daemon, a handler, a fixture
  registry or a second method table.
- ``x-hyprial-examples`` and ``x-hyprial-example-gaps`` are re-validated here
  against the projected params/result/error schemas.  The exporter already
  validated them; this consumer check keeps a hand-edited artifact from turning
  into an accidental successful mock response.
- The scenario vocabulary is exactly success, empty, unconfigured and error.
  The error scenario serves either a declared domain-error or a schema-valid
  semantic-param-error; the kind declares the validation boundary and the code
  is not used to infer it.  Selecting a scenario a method does not declare is an
  explicit 409, never a fabricated payload.
- Case selection, selectors and the served mock moved to the JavaScript Contract
  Mock (gui/contract-mock, 1e-2, 2026-11-15); this package keeps what Python
  alone can check: load-time validation of every example against the projected
  schemas with jsonschema, the sensitive-material scan and envelope validation
  (checks.py), and example lookup by name for tests of the exporters.
- Decision update (2026-11-21): validate ``x-hyprial-mock-echo`` on each
  method contract using the same structural rules as ``gui/contract-mock``
  (non-empty string->string mapping over declared param and result properties).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCENARIOS = ("success", "empty", "unconfigured", "error")
SCENARIO_KINDS = {
    "success": ("success",),
    "empty": ("empty",),
    "unconfigured": ("unconfigured",),
    "error": ("domain-error", "semantic-param-error"),
}
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
PARAM_ERROR_CODES = frozenset({"INVALID_ARGUMENT", "INVALID_PARAMS", "INVALID_REQUEST"})
DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "openapi.json"
DEFAULT_ENVELOPE = Path(__file__).resolve().parents[1] / "http-envelope.schema.json"
_MISSING = object()


class ContractError(ValueError):
    """The projection cannot be used as a mock contract."""


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _validator(schema: MappingLike, where: str) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(_plain(schema))
    except Exception as error:
        raise ContractError(f"{where}: invalid JSON schema: {error}") from error
    return Draft202012Validator(_plain(schema), format_checker=FormatChecker())


MappingLike = dict[str, Any]


@dataclass(frozen=True, slots=True)
class MethodContract:
    name: str
    path: str
    params_schema: MappingLike
    result_schema: MappingLike
    errors: tuple[MappingLike, ...]
    examples: tuple[MappingLike, ...]
    gaps: MappingLike
    echo: MappingLike = field(default_factory=dict)

    def cases_for(self, scenario: str) -> tuple[MappingLike, ...]:
        kinds = SCENARIO_KINDS[scenario]
        return tuple(case for case in self.examples if case["kind"] in kinds)

    def gap_for(self, scenario: str) -> str | None:
        key = {"success": "success", "empty": "empty", "error": "domainErrors"}.get(scenario)
        if key is None:
            return None
        value = self.gaps.get(key)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class Contract:
    openapi_version: str
    contract_version: str
    methods: dict[str, MethodContract]
    skipped: MappingLike
    envelope_schema: MappingLike

    def case(self, method: str, name: str) -> MappingLike:
        contract = self.methods.get(method)
        if contract is None:
            raise ContractError(f"unknown method {method!r}")
        for example in contract.examples:
            if example["name"] == name:
                return example
        raise ContractError(f"{method}: no example case named {name!r}")


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {label} {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractError(f"{label} {path} is not JSON: {error}") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _error_map(errors: list[MappingLike]) -> dict[str, Any]:
    declared: dict[str, Any] = {}
    for error in errors:
        _require(isinstance(error, dict), "declared error is not an object")
        code = error.get("code")
        _require(isinstance(code, str) and bool(code), "declared error has no code")
        declared[code] = error.get("dataSchema")
    return declared


def _check_error_case(
    case: MappingLike,
    error: MappingLike,
    declared: dict[str, Any],
    *,
    kind: str,
) -> None:
    code = error.get("code")
    message = error.get("message")
    _require(isinstance(code, str) and bool(code), f"{case.get('name')}: error code")
    _require(
        isinstance(message, str) and bool(message) and "\n" not in message and "\r" not in message,
        f"{case.get('name')}: error message",
    )
    _require(len(message) <= 512, f"{case.get('name')}: error message is too long")
    _require(code in declared, f"{case.get('name')}: error {code!r} is not declared")
    data_schema = declared[code]
    if "data" not in error:
        return
    _require(data_schema is not None, f"{case.get('name')}: declared {code} has no data schema")
    data = error["data"]
    _require(isinstance(data, dict), f"{case.get('name')}: error data must be an object")
    validator = _validator(data_schema, f"{kind} error data {code}")
    _require(
        validator.is_valid(_plain(data)),
        f"{case.get('name')}: error data does not match {code}",
    )


def _validate_examples(contract: MethodContract) -> None:
    _require(bool(contract.examples), f"{contract.name}: no x-hyprial-examples")
    params_validator = _validator(contract.params_schema, f"{contract.name}.params")
    result_validator = _validator(contract.result_schema, f"{contract.name}.result")
    declared = _error_map(list(contract.errors))
    names: set[str] = set()
    kinds: set[str] = set()
    for case in contract.examples:
        _require(isinstance(case, dict), f"{contract.name}: example is not an object")
        name = case.get("name")
        kind = case.get("kind")
        description = case.get("description")
        _require("params" in case, f"{name}: params is required")
        params = case["params"]
        _require(isinstance(name, str) and bool(name), f"{contract.name}: example name")
        _require(name not in names, f"{contract.name}: duplicate example {name!r}")
        names.add(name)
        _require(kind in EXAMPLE_KINDS, f"{name}: unknown example kind {kind!r}")
        _require(
            isinstance(description, str) and bool(description),
            f"{name}: description is required",
        )
        if kind != "invalid-params":
            _require(
                isinstance(params, dict), f"{name}: {kind} params must be an object"
            )
        valid_params = params_validator.is_valid(_plain(params))
        kinds.add(kind)
        if kind == "invalid-params":
            _require(not valid_params, f"{name}: invalid-params params actually validate")
            _require("result" not in case, f"{name}: invalid-params cannot carry a result")
            error = case.get("error")
            _require(isinstance(error, dict), f"{name}: invalid-params needs an error")
            _require(
                error.get("code") in PARAM_ERROR_CODES,
                f"{name}: invalid-params must use a parameter error code",
            )
            _check_error_case(case, error, declared, kind=kind)
            continue
        if kind in ("semantic-param-error", "domain-error"):
            _require(
                valid_params,
                f"{name}: {kind} params must validate; schema-invalid belongs to "
                "invalid-params",
            )
            _require("result" not in case, f"{name}: {kind} cannot carry a result")
            error = case.get("error")
            _require(isinstance(error, dict), f"{name}: {kind} needs an error")
            _check_error_case(case, error, declared, kind=kind)
            continue
        _require(valid_params, f"{name}: example params must validate")
        _require("error" not in case, f"{name}: {kind} cannot carry an error")
        result = case.get("result")
        _require(isinstance(result, dict), f"{name}: {kind} needs a result object")
        _require(
            result_validator.is_valid(_plain(result)),
            f"{name}: example result must validate",
        )
        if kind in ("empty", "unconfigured"):
            reason = case.get("reason")
            _require(
                isinstance(reason, str) and bool(reason),
                f"{name}: {kind} needs a reason",
            )
        else:
            _require("reason" not in case, f"{name}: reason is only for empty/unconfigured")
    _require(
        "success" in kinds or bool(contract.gap_for("success")),
        f"{contract.name}: no success example or gap reason",
    )
    if "success" in kinds:
        _require(not contract.gap_for("success"), f"{contract.name}: success gap contradicts a case")
    _require("invalid-params" in kinds, f"{contract.name}: no invalid-params example")
    has_empty = bool(kinds & {"empty", "unconfigured"})
    _require(
        has_empty or bool(contract.gap_for("empty")),
        f"{contract.name}: no empty/unconfigured example or gap reason",
    )
    error_kinds = kinds & {"domain-error", "semantic-param-error"}
    _require(
        bool(error_kinds) or bool(contract.gap_for("error")),
        f"{contract.name}: no domain-error/semantic-param-error example or gap reason",
    )
    if has_empty:
        _require(not contract.gap_for("empty"), f"{contract.name}: empty gap contradicts case")
    if error_kinds:
        _require(
            not contract.gap_for("error"),
            f"{contract.name}: error gap contradicts a declared error case",
        )


def _check_echo(
    name: str,
    raw_echo: Any,
    params_schema: MappingLike,
    result_schema: MappingLike,
) -> MappingLike:
    if raw_echo is _MISSING:
        return {}
    _require(isinstance(raw_echo, dict), f"{name}: x-hyprial-mock-echo is not an object")
    _require(bool(raw_echo), f"{name}: x-hyprial-mock-echo must not be empty")
    params_props = params_schema.get("properties")
    result_props = result_schema.get("properties")
    _require(
        isinstance(params_props, dict) and isinstance(result_props, dict),
        f"{name}: x-hyprial-mock-echo requires object schemas",
    )
    checked: dict[str, str] = {}
    for param_key, result_key in sorted(raw_echo.items()):
        _require(
            isinstance(param_key, str) and bool(param_key) and param_key in params_props,
            f"{name}: x-hyprial-mock-echo param {param_key!r} is not declared",
        )
        _require(
            isinstance(result_key, str) and bool(result_key) and result_key in result_props,
            f"{name}: x-hyprial-mock-echo result {result_key!r} is not declared",
        )
        checked[param_key] = result_key
    return checked


def _method_contract(name: str, path: str, operation: MappingLike) -> MethodContract:
    _require(isinstance(operation, dict), f"{name}: operation is not an object")
    request = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    params_schema = request.get("schema")
    result = operation.get("responses", {}).get("200", {}).get("content", {}).get(
        "application/json", {}
    ).get("schema")
    _require(isinstance(params_schema, dict), f"{name}: params schema is missing")
    _require(isinstance(result, dict), f"{name}: result schema is missing")
    errors = operation.get("x-hyprial-errors")
    if errors is None:
        errors = []
        for variant in _error_variants(operation):
            properties = variant.get("properties")
            if not isinstance(properties, dict):
                continue
            code = properties.get("code")
            if isinstance(code, dict) and isinstance(code.get("const"), str):
                errors.append(
                    {
                        "code": code["const"],
                        "dataSchema": properties.get("data"),
                    }
                )
    _require(isinstance(errors, list), f"{name}: error declarations are missing")
    examples = operation.get("x-hyprial-examples")
    _require(isinstance(examples, list), f"{name}: x-hyprial-examples is missing")
    gaps = operation.get("x-hyprial-example-gaps", {})
    _require(isinstance(gaps, dict), f"{name}: x-hyprial-example-gaps is not an object")
    echo = _check_echo(
        name, operation.get("x-hyprial-mock-echo", _MISSING), params_schema, result
    )
    return MethodContract(
        name=name,
        path=path,
        params_schema=params_schema,
        result_schema=result,
        errors=tuple(error for error in errors if isinstance(error, dict) and error.get("code")),
        examples=tuple(examples),
        gaps=gaps,
        echo=echo,
    )


def _error_variants(operation: MappingLike) -> list[MappingLike]:
    schema = (
        operation.get("responses", {})
        .get("default", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    if not isinstance(schema, dict):
        return []
    variants = schema.get("oneOf")
    if isinstance(variants, list):
        return [item for item in variants if isinstance(item, dict)]
    return [schema] if schema else []


def load_contract(
    path: Path = DEFAULT_CONTRACT, envelope_path: Path = DEFAULT_ENVELOPE
) -> Contract:
    document = _read_json(path, "business OpenAPI")
    _require(isinstance(document, dict), "business OpenAPI must be an object")
    _require(document.get("openapi") == "3.1.0", "business OpenAPI must be 3.1.0")
    info = document.get("info")
    _require(isinstance(info, dict), "business OpenAPI has no info")
    paths = document.get("paths")
    _require(isinstance(paths, dict), "business OpenAPI has no paths")
    methods: dict[str, MethodContract] = {}
    skipped: dict[str, str] = {}
    for path, entry in paths.items():
        _require(isinstance(entry, dict), f"{path}: path item is not an object")
        operation = entry.get("post")
        _require(isinstance(operation, dict), f"{path}: post operation is missing")
        name = operation.get("operationId")
        _require(isinstance(name, str) and bool(name), f"{path}: operationId is missing")
        _require(path == f"/daemon/request/{name}", f"{path}: operationId/path mismatch")
        raw_examples = operation.get("x-hyprial-examples")
        if not isinstance(raw_examples, list) or not raw_examples:
            skipped[name] = "no x-hyprial-examples"
            continue
        contract = _method_contract(name, path, operation)
        _validate_examples(contract)
        methods[name] = contract
    for entry in document.get("x-hyprial-methods", []):
        name = entry["name"]
        declaration_only = not entry.get("httpExposed")
        if name in methods or not declaration_only or not entry.get("x-hyprial-examples"):
            continue
        contract = MethodContract(
            name=name, path=f"/daemon/request/{name}",
            params_schema=entry["paramsSchema"], result_schema=entry["resultSchema"],
            errors=tuple(entry["errors"]), examples=tuple(entry["x-hyprial-examples"]),
            gaps=entry.get("x-hyprial-example-gaps", {}),
            echo=_check_echo(
                name,
                entry.get("x-hyprial-mock-echo", _MISSING),
                entry["paramsSchema"],
                entry["resultSchema"],
            ),
        )
        _validate_examples(contract)
        methods[name] = contract
        skipped.pop(name, None)
    _require(
        bool(methods),
        "business OpenAPI has no mockable methods; no path has x-hyprial-examples",
    )
    envelope = _read_json(envelope_path, "HTTP envelope schema")
    _require(isinstance(envelope, dict), "HTTP envelope schema must be an object")
    _require(isinstance(envelope.get("$defs"), dict), "HTTP envelope schema has no $defs")
    return Contract(
        openapi_version=document["openapi"],
        contract_version=str(info.get("version", "")),
        methods=methods,
        skipped=skipped,
        envelope_schema=envelope,
    )
