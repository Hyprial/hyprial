#!/usr/bin/env python3
"""Validate the single HTTP contract corpus and, optionally, real documents.

The corpus is the frozen source of truth for both the Python server tests and
the future TypeScript client.  ``--catalog``/``--response`` accept captured
real documents; without them this script only checks corpus self-consistency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
CONTRACT = Path(__file__).with_name("http-contract.v1.json")

from jsonschema import Draft202012Validator  # noqa: E402
from jsonschema.exceptions import SchemaError  # noqa: E402

from hyprial.shell.impl.request_http.errors import (  # noqa: E402
    RETRYABLE_CODES,
    STATUS_BY_CODE,
)


def _load() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def problems(document: dict) -> list[str]:
    issues: list[str] = []
    if document.get("version") != 1 or document.get("httpContractVersion") != 1:
        issues.append("version/httpContractVersion must be 1")
    dispatch = document.get("dispatch", {})
    if dispatch.get("values") != ["not-dispatched", "dispatched", "unknown"]:
        issues.append("dispatch values are not the closed three-state set")
    if dispatch.get("outcomeUnknown") != ["unknown"]:
        issues.append("only dispatch=unknown may set outcomeUnknown")
    if document.get("statusByCode") != dict(STATUS_BY_CODE):
        issues.append("statusByCode differs from the server's closed map")
    if document.get("retryableCodes") != sorted(RETRYABLE_CODES):
        issues.append("retryableCodes differs from the server's transient set")
    for name in ("successEnvelope", "errorEnvelope", "catalog"):
        schema = document.get(name)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            issues.append(f"{name}: {error.message}")
    headers = document.get("headers", {})
    if headers.get("catalogDigest") != "X-Hyprial-Catalog-Digest":
        issues.append("catalog digest header changed")
    if headers.get("requestId") != "X-Hyprial-Request-Id":
        issues.append("request id header changed")
    if "OPTIONS /readyz|/daemon/request|/daemon/request/<method>" not in document.get("endpoints", {}):
        issues.append("protected GET preflight is not declared")
    if document.get("retryRules", {}).get("serverRetries") is not False:
        issues.append("the server must never retry")
    return issues


def validate_catalog(path: Path) -> list[str]:
    document = _load()
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues = [
        f"catalog: {error.message}"
        for error in Draft202012Validator(document["catalog"]).iter_errors(payload)
    ]
    for method in payload.get("methods", []):
        if method.get("state") not in {"available", "not-ready"}:
            issues.append(f"catalog: method {method.get('name')} has a non-callable state")
        binding = method.get("httpBinding") or {}
        if (binding.get("identity") or {}).get("mode") not in document["identity"]["modes"]:
            issues.append(f"catalog: method {method.get('name')} has an unsupported identity")
    return issues


def validate_response(path: Path) -> list[str]:
    document = _load()
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema = document["successEnvelope"] if payload.get("ok") else document["errorEnvelope"]
    return [
        f"response: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(payload)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--response", type=Path)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"note: ignoring runner arguments {unknown}", file=sys.stderr)
    issues = problems(_load())
    if args.catalog:
        issues.extend(validate_catalog(args.catalog))
    if args.response:
        issues.extend(validate_response(args.response))
    if issues:
        for issue in issues:
            print(f"FAIL {issue}", file=sys.stderr)
        return 1
    print("PASS http-request contract corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
