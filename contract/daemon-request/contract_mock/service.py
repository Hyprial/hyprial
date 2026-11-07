"""Contract Mock request handling without a daemon or HTTP server.

Decision record (M-03, 2026-10-31)
- The service is the testable middle: it owns auth, parameter validation,
  explicit scenario selection and envelope construction, while the HTTP handler
  owns only framing.  Tests exercise both layers.
- Parameter validation runs before scenario selection, mirroring the transport
  rule that a schema-malformed request is not dispatched.  A selected
  domain-error or schema-valid semantic-param-error example is a dispatch result,
  not a pre-dispatch refusal.  The kind declares which boundary produced the
  error; the mock does not infer it from the error code.
- A method that has no case for the selected scenario returns an explicit
  ``MOCK_SCENARIO_MISSING`` 409 with the recorded gap reason.  It never falls
  back to success or invents an empty payload.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from .contract import Contract, ContractError, SCENARIOS
from .envelope import Envelope
from .sensitive import SENTINEL, SENTINEL_B64, SensitiveMaterialError, assert_no_sensitive_material

MOCK_SCENARIO_MISSING = "MOCK_SCENARIO_MISSING"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    payload: dict[str, Any]


class MockService:
    def __init__(
        self,
        contract: Contract,
        envelope: Envelope,
        *,
        scenario: str,
        token: str,
        method_scenarios: dict[str, str] | None = None,
        method_cases: dict[str, str] | None = None,
        request_id: Callable[[], str] | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ContractError(f"unknown scenario {scenario!r}")
        if not isinstance(token, str) or not token:
            raise ContractError("the mock token must be a non-empty string")
        self.contract = contract
        self.envelope = envelope
        self.scenario = scenario
        self._authorization = f"Bearer {token}"
        self._request_id = request_id or envelope.request_id
        self._method_scenarios = self._validate_scenarios(method_scenarios or {})
        self._method_cases = self._validate_cases(method_cases or {})
        self._params = {
            name: Draft202012Validator(
                method.params_schema, format_checker=FormatChecker()
            )
            for name, method in contract.methods.items()
        }
        assert_no_sensitive_material(
            {
                name: list(method.examples)
                for name, method in contract.methods.items()
            }
        )
        self._validate_all_responses()

    def _validate_scenarios(self, overrides: dict[str, str]) -> dict[str, str]:
        checked: dict[str, str] = {}
        for method, scenario in overrides.items():
            if method not in self.contract.methods:
                raise ContractError(f"{method}: method-scenario names an unknown method")
            if scenario not in SCENARIOS:
                raise ContractError(f"{method}: unknown scenario {scenario!r}")
            if self.contract.select(method, scenario) is None:
                gap = self.contract.methods[method].gap_for(scenario)
                detail = f": {gap}" if gap else ""
                raise ContractError(
                    f"{method}: no {scenario} example for the explicit override{detail}"
                )
            checked[method] = scenario
        return checked

    def _validate_cases(self, overrides: dict[str, str]) -> dict[str, str]:
        checked: dict[str, str] = {}
        for method, case_name in overrides.items():
            if method not in self.contract.methods:
                raise ContractError(f"{method}: case override names an unknown method")
            self.contract.select(method, self.scenario, case_name)
            checked[method] = case_name
        return checked

    def _validate_all_responses(self) -> None:
        for name, method in self.contract.methods.items():
            for case in method.examples:
                if case["kind"] in ("domain-error", "semantic-param-error"):
                    error = case["error"]
                    self.envelope.error(
                        "mock-startup",
                        error["code"],
                        error["message"],
                        "dispatched",
                        method=name,
                        data=error.get("data"),
                    )
                elif case["kind"] == "invalid-params":
                    error = case["error"]
                    self.envelope.error(
                        "mock-startup",
                        error["code"],
                        error["message"],
                        "not-dispatched",
                        method=name,
                        data=error.get("data"),
                    )
                else:
                    self.envelope.success(name, "mock-startup", case["result"])

    def authorized(self, header: str | None) -> bool:
        if not isinstance(header, str):
            return False
        return hmac.compare_digest(header, self._authorization)

    def unauthorized(self) -> Response:
        return self.error_response(
            "HTTP_UNAUTHORIZED", "a mock bearer token is required", status=401
        )

    def error_response(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        dispatch: str = "not-dispatched",
        method: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Response:
        payload = self.envelope.error(
            self._request_id(),
            code,
            message,
            dispatch,
            method=method,
            data=data,
        )
        return Response(
            self.envelope.status_for(code) if status is None else status,
            payload,
        )

    def invalid_json(self) -> Response:
        return self.error_response(
            "INVALID_REQUEST", "request body must be a JSON object", status=400
        )

    def unsupported_methods(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in sorted(self.contract.methods)
            if self.contract.select(
                name, self._method_scenarios.get(name, self.scenario),
                self._method_cases.get(name),
            )
            is None
        )

    def handle(
        self, method: str, params: object, authorization: str | None
    ) -> Response:
        if not self.authorized(authorization):
            return self.unauthorized()
        contract = self.contract.methods.get(method)
        if contract is None:
            return self.error_response(
                "METHOD_NOT_FOUND",
                "unknown daemon method",
                status=404,
                method=method,
            )
        if not isinstance(params, dict):
            return self.error_response(
                "INVALID_ARGUMENT",
                "params must be a JSON object",
                status=400,
                method=method,
            )
        if method == "agent.provider.login":
            files = params.get("files")
            synthetic = params.get("setupToken") == SENTINEL or (
                isinstance(files, list) and bool(files) and all(
                    isinstance(item, dict) and item.get("contentBase64") == SENTINEL_B64
                    for item in files
                )
            )
            try:
                assert_no_sensitive_material(params)
            except SensitiveMaterialError:
                synthetic = False
            if not synthetic:
                return self.error_response(
                    "INVALID_ARGUMENT", "mock login accepts only the documented synthetic sentinel",
                    status=400, method=method,
                )
        errors = sorted(
            self._params[method].iter_errors(params),
            key=lambda error: list(error.path),
        )
        if errors:
            path = "/".join(str(part) for part in errors[0].absolute_path) or "<root>"
            return self.error_response(
                "INVALID_ARGUMENT",
                f"{method} params do not match the declared schema at {path}",
                status=400,
                method=method,
            )
        case = self.selected_case(method)
        if case is None:
            return self._missing_scenario(method)
        if case["kind"] in ("domain-error", "semantic-param-error"):
            error = case["error"]
            return self.error_response(
                error["code"],
                error["message"],
                dispatch="dispatched",
                method=method,
                data=error.get("data"),
            )
        return Response(
            200,
            self.envelope.success(method, self._request_id(), case["result"]),
        )

    def selected_case(self, method: str) -> dict[str, Any] | None:
        scenario = self._method_scenarios.get(method, self.scenario)
        return self.contract.select(
            method, scenario, self._method_cases.get(method)
        )

    def _missing_scenario(self, method: str) -> Response:
        scenario = self._method_scenarios.get(method, self.scenario)
        contract = self.contract.methods[method]
        gap = contract.gap_for(scenario)
        available = ", ".join(contract.available_scenarios()) or "none"
        detail = f" Recorded reason: {gap}." if gap else ""
        return self.error_response(
            MOCK_SCENARIO_MISSING,
            f"{method} has no {scenario} example (available: {available}).{detail}",
            status=409,
            method=method,
        )

    def contract_document(self) -> dict[str, Any]:
        methods: dict[str, Any] = {}
        for name, method in sorted(self.contract.methods.items()):
            methods[name] = {
                "path": method.path,
                "scenarios": list(method.available_scenarios()),
                "gaps": dict(method.gaps),
            }
        return {
            "mock": True,
            "contractVersion": self.contract.contract_version,
            "contractDigest": self.contract.digest,
            "openapiVersion": self.contract.openapi_version,
            "scenario": self.scenario,
            "methodScenarios": dict(sorted(self._method_scenarios.items())),
            "methodCases": dict(sorted(self._method_cases.items())),
            "unsupportedForScenario": list(self.unsupported_methods()),
            "envelopeSource": self.contract.envelope_source,
            "methods": methods,
            "skipped": dict(sorted(self.contract.skipped.items())),
        }
