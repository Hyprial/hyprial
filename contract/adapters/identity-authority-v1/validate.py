#!/usr/bin/env python3
"""Validate identity/authorization corpus rows and closed authority names."""

from __future__ import annotations

import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
ALLOWED = {"organization", "management", "service", "claim"}
REFUSAL_CODES = {
    "CLAIM_AUTHORIZATION_REQUIRED", "CLAIM_AUTHORIZATION_INVALID",
    "CLAIM_AUTHORIZATION_EXPIRED", "CLAIM_AUTHORIZATION_REVOKED",
    "CLAIM_AUTHORIZATION_ISSUER_UNAVAILABLE", "CLAIM_AUTHORIZATION_USE_CONFLICT",
    "CLAIM_OPERATION_CONFLICT", "CLAIM_GENERATION_MISMATCH", "CLAIM_REQUEST_INVALID",
}
REFUSAL_DISPOSITIONS = {
    "held-retry-after-trust", "terminal-new-authorization",
    "terminal-correct-request", "terminal-conflict",
}


def main() -> int:
    seen: set[str] = set()
    authority_numbers: set[int] = set()
    for name in ("cases.jsonl", "org-lease-claim-generation.jsonl"):
        for number, line in enumerate(HERE.joinpath(name).read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)
            if set(row) != {"schemaVersion", "caseId", "authority", "expected"}:
                raise SystemExit(f"invalid keys: {name}:{number}")
            if row["schemaVersion"] != 1 or row["authority"] not in ALLOWED:
                raise SystemExit(f"invalid authority: {name}:{number}")
            if not re.fullmatch(r"UA-\d{3}:[a-z0-9-]+", row["caseId"]):
                raise SystemExit(f"invalid case id: {name}:{number}")
            if row["caseId"] in seen or not isinstance(row["expected"], dict):
                raise SystemExit(f"duplicate or invalid case: {name}:{number}")
            seen.add(row["caseId"])
            if name == "org-lease-claim-generation.jsonl":
                ua = int(row["caseId"][3:6])
                authority_numbers.add(ua)
                expected = row["expected"]
                if 403 <= ua <= 416 or 459 <= ua <= 466:
                    required = {"context", "kind", "signature", "action", "resource", "digest", "useRows"}
                elif 417 <= ua <= 430:
                    required = {"issuer", "root", "frontier", "notBefore", "expiresAt", "action", "resource", "digest", "useRows", "claimRows", "resultRows"}
                elif 431 <= ua <= 448:
                    required = {"issuer", "root", "frontier", "notBefore", "expiresAt", "kind", "signature", "action", "resource", "digest", "useRows", "manifestRows"}
                elif 449 <= ua <= 458:
                    required = {"activationChallenge", "currentFrontier", "signature", "state", "activationRows", "effectRows"}
                else:
                    required = {"wireCode", "reason", "disposition", "signature", "localCodeExcluded", "claimRows"}
                if not required <= set(expected):
                    missing = sorted(required - set(expected))
                    raise SystemExit(f"missing authority fields {missing}: {name}:{number}")
                if ua >= 467 and (not expected["localCodeExcluded"] or expected["claimRows"] != 0):
                    raise SystemExit(f"invalid closed refusal: {name}:{number}")
                if ua >= 467 and (
                    expected["wireCode"] not in REFUSAL_CODES
                    or expected["disposition"] not in REFUSAL_DISPOSITIONS
                    or not isinstance(expected["reason"], str)
                    or not expected["reason"]
                ):
                    raise SystemExit(f"invalid refusal vocabulary: {name}:{number}")
    if authority_numbers != set(range(403, 473)):
        raise SystemExit("org-lease-claim-generation.jsonl must own exactly UA-403..472")
    print(f"RESULT PASS identity-authority-v1 cases={len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
