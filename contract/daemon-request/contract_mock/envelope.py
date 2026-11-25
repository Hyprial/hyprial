"""HTTP transport envelope validation for the Contract Mock examples.

Decision record (M-03, 2026-10-31; 1e-2, 2026-11-15)
- The mock must speak the same outer shape as the real H transport, otherwise a
  generated client would need a mock-only branch.  This module is a temporary
  adapter pinned to the H-01 candidate envelope; ``http-envelope.schema.json``
  is the reviewable artifact and HM-01 owns the combined replacement check.
- Every example is built into an envelope and validated against that schema by
  ``check_contract`` (checks.py), so a malformed example fails the check instead
  of becoming a plausible-looking success.  The served mock is JavaScript
  (gui/contract-mock); its replies are validated against the same schema by its
  own conformance test.  Request identifiers and bearer tokens are no longer
  minted here: the server that needed them is gone.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from .contract import Contract, ContractError

SCHEMA_VERSION = 1


class Envelope:
    def __init__(self, contract: Contract) -> None:
        definitions = contract.envelope_schema.get("$defs")
        if not isinstance(definitions, dict):
            raise ContractError("HTTP envelope schema has no $defs")
        success = definitions.get("success")
        error = definitions.get("error")
        if not isinstance(success, dict) or not isinstance(error, dict):
            raise ContractError("HTTP envelope schema needs success and error definitions")
        self._success = Draft202012Validator(success)
        self._error = Draft202012Validator(error)

    @staticmethod
    def _message(value: str) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ")
        return text[:512]

    def success(self, method: str, request_id: str, result: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "ok": True,
            "method": method,
            "requestId": request_id,
            "dispatch": "dispatched",
            "result": result,
        }
        self._require(self._success, payload, "success")
        return payload

    def error(
        self,
        request_id: str,
        code: str,
        message: str,
        dispatch: str = "not-dispatched",
        *,
        method: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "ok": False,
            "requestId": request_id,
            "code": code,
            "message": self._message(message),
            "dispatch": dispatch,
        }
        if method is not None:
            payload["method"] = method
        if data is not None:
            payload["data"] = data
        self._require(self._error, payload, "error")
        return payload

    @staticmethod
    def _require(validator: Draft202012Validator, payload: dict[str, Any], kind: str) -> None:
        errors = sorted(validator.iter_errors(payload), key=lambda item: item.path)
        if errors:
            raise ContractError(
                f"{kind} envelope violates the pinned HTTP schema: {errors[0].message}"
            )
