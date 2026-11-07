"""HTTP transport envelope for the Contract Mock.

Decision record (M-03, 2026-10-31)
- The mock must speak the same outer shape as the real H transport, otherwise a
  generated client would need a mock-only branch.  This module is a temporary
  adapter pinned to the H-01 candidate envelope; ``http-envelope.schema.json``
  is the reviewable artifact and HM-01 owns the combined replacement check.
- Every payload is validated against that schema before it leaves the process.
  A malformed example therefore fails at startup or as a 500, never as a
  plausible-looking success.
- Request identifiers, messages and the synthetic bearer token are generated
  locally.  No daemon, home, identity, credential store or real token is read.
"""

from __future__ import annotations

import secrets
import uuid
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
        self._success_schema = success
        self._error_schema = error
        self._success = Draft202012Validator(success)
        self._error = Draft202012Validator(error)
        statuses = contract.envelope_schema.get("x-hyprial-status-by-code")
        self._statuses = statuses if isinstance(statuses, dict) else {}
        default = contract.envelope_schema.get("x-hyprial-status-default", 500)
        self._default_status = default if isinstance(default, int) else 500

    @staticmethod
    def request_id() -> str:
        return "mock-" + uuid.uuid4().hex[:16]

    @staticmethod
    def token() -> str:
        return "mock-not-a-credential-" + secrets.token_hex(8)

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

    def status_for(self, code: str) -> int:
        value = self._statuses.get(code)
        return value if isinstance(value, int) else self._default_status

    @staticmethod
    def _require(validator: Draft202012Validator, payload: dict[str, Any], kind: str) -> None:
        errors = sorted(validator.iter_errors(payload), key=lambda item: item.path)
        if errors:
            raise ContractError(
                f"{kind} envelope violates the pinned HTTP schema: {errors[0].message}"
            )
