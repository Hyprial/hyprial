"""Private authorization receipts contain no credentials or handoff URLs.

The daemon's home lease owns this journal. Admission is persisted before a
worker can start. A recovered nonterminal receipt is unknown and never resumes
an external write. Atomic persistence failures fence further submissions.
"""

from copy import deepcopy
import json
from pathlib import Path

from hyprial.kernel import read_private_file, secure_private_directory, write_private_file_atomic

STATES = {"starting", "awaiting_user", "checking", "succeeded", "denied", "expired", "cancelled", "failed", "unknown"}
FIELDS = {"operationId", "operationKey", "adapter", "appId", "status", "authorization", "capabilities", "errorCode"}


class AuthorizationReceiptError(RuntimeError):
    """No new effect is admitted while its durable result is uncertain."""


class AuthorizationReceipts:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.failed = False

    def load(self) -> dict[str, dict]:
        if self.path is None:
            return {}
        try:
            secure_private_directory(self.path.parent, create=True)
            raw, _ = read_private_file(self.path, max_bytes=1024 * 1024)
        except FileNotFoundError:
            return {}
        except Exception as error:
            raise AuthorizationReceiptError("authorization receipt unavailable") from error
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"version", "operations"} or value["version"] != 1:
                raise ValueError("invalid journal")
            operations = value["operations"]
            if not isinstance(operations, list) or len(operations) > 128:
                raise ValueError("invalid operations")
            result = {}
            keys = set()
            for op in operations:
                if (not isinstance(op, dict) or not set(op) <= FIELDS
                        or not all(isinstance(op.get(field), str) and op[field] for field in ("operationId", "adapter", "status"))
                        or op["status"] not in STATES or not isinstance(op.get("authorization"), dict)
                        or op["operationId"] in result):
                    raise ValueError("invalid receipt")
                key = op.get("operationKey")
                if key is not None:
                    if not isinstance(key, str) or not 1 <= len(key) <= 128 or key in keys:
                        raise ValueError("invalid operation key")
                    keys.add(key)
                if op["status"] in {"starting", "awaiting_user", "checking", "unknown"}:
                    op.update(status="unknown", errorCode="AUTHORIZATION_RECOVERY_REQUIRED")
                result[op["operationId"]] = op
            return result
        except (ValueError, TypeError, KeyError) as error:
            raise AuthorizationReceiptError("authorization receipt invalid") from error

    def save(self, operations: dict[str, dict]) -> None:
        if self.path is None:
            return
        if self.failed:
            raise AuthorizationReceiptError("authorization receipt recovery required")
        try:
            rows = [{key: deepcopy(value) for key, value in op.items() if key in FIELDS}
                    for op in operations.values()]
            data = json.dumps({"version": 1, "operations": rows}).encode("utf-8")
            if len(data) > 1024 * 1024:
                raise ValueError("journal capacity exceeded")
            write_private_file_atomic(self.path, data)
        except Exception as error:
            self.failed = True
            raise AuthorizationReceiptError("authorization receipt write unconfirmed") from error
