"""Creation receipts are private, bounded and contain neither secrets nor URLs.

An interrupted creation may have created a remote app. Recovery never retries
the external request or interprets local cancellation as remote rollback.
"""

from copy import deepcopy
import json
from pathlib import Path

from hyprial.kernel import read_private_file, secure_private_directory, write_private_file_atomic

ACTIVE = {"starting", "awaiting_user", "checking"}
STATES = ACTIVE | {"succeeded", "denied", "expired", "cancelled", "failed", "unknown"}
FIELDS = {"operationId", "operationKey", "adapter", "provider", "kind", "status", "configured",
          "reloaded", "remoteOutcomeUnknown", "appId", "errorCode", "nextAction"}


class OnboardingReceiptError(RuntimeError):
    """Durable state is unavailable; no new creation may be submitted."""


class OnboardingJournal:
    def __init__(self, path: Path):
        self.path = path
        self.failed = False

    def load(self):
        try:
            secure_private_directory(self.path.parent, create=True)
            raw, _ = read_private_file(self.path, max_bytes=1024 * 1024)
        except FileNotFoundError:
            return {}
        except Exception as error:
            raise OnboardingReceiptError("onboarding journal unavailable") from error
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"version", "operations"} or value["version"] != 1:
                raise ValueError("invalid journal")
            rows = value["operations"]
            if not isinstance(rows, list) or len(rows) > 128:
                raise ValueError("invalid operations")
            result, keys = {}, set()
            for row in rows:
                if (not isinstance(row, dict) or not set(row) <= FIELDS
                        or not all(isinstance(row.get(key), str) and row[key] for key in ("operationId", "operationKey", "adapter"))
                        or row.get("status") not in STATES or row.get("provider") != "lark" or row.get("kind") != "add"
                        or not all(isinstance(row.get(key), bool) for key in ("configured", "reloaded", "remoteOutcomeUnknown"))
                        or row["operationId"] in result or row["operationKey"] in keys):
                    raise ValueError("invalid receipt")
                keys.add(row["operationKey"])
                if row["status"] in ACTIVE | {"unknown"}:
                    row.update(status="unknown", remoteOutcomeUnknown=True, errorCode="APP_CREATION_RECOVERY_REQUIRED", nextAction="inspect")
                result[row["operationId"]] = row
            return result
        except (ValueError, TypeError, KeyError) as error:
            raise OnboardingReceiptError("onboarding journal invalid") from error

    def save(self, operations):
        if self.failed:
            raise OnboardingReceiptError("onboarding journal recovery required")
        try:
            rows = [{key: deepcopy(value) for key, value in row.items() if key in FIELDS} for row in operations.values()]
            data = json.dumps({"version": 1, "operations": rows}).encode()
            if len(data) > 1024 * 1024:
                raise ValueError("journal capacity exceeded")
            write_private_file_atomic(self.path, data)
        except Exception as error:
            self.failed = True
            raise OnboardingReceiptError("onboarding receipt write unconfirmed") from error
