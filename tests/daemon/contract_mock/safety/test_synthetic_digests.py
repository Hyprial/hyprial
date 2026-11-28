"""Only reproducible public fixture hashes may pass the long-string scanner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from contract_mock.sensitive import SENTINEL, scan_value

ROOT = Path(__file__).resolve().parents[4]
PLAN_PATH = ROOT / "tests/fixtures/contract_mock/synthetic-digests/plan.json"
PLAN_ID = "b9d31a5e951e7342d974a2491e41fe4e04624c0a685364d94412e43c4b31f878"
BLOB_ID = hashlib.sha256(SENTINEL.encode()).hexdigest()


def test_public_plan_preimage_reproduces_the_exact_allowed_digest() -> None:
    plan = json.loads(PLAN_PATH.read_text())
    raw = json.dumps(plan["unsigned"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(raw.encode()).hexdigest() == PLAN_ID == plan["planId"]
    assert scan_value({"plan_id": PLAN_ID}) == []


def test_only_the_public_sentinel_content_hash_is_allowed() -> None:
    assert scan_value({"blob_hash": BLOB_ID, "sha": BLOB_ID}) == []


@pytest.mark.parametrize("value", ["a" * 64, "b" * 64, "A" * 80, PLAN_ID[:-1] + "0", BLOB_ID[:-1] + "0"])
def test_other_digest_shaped_values_remain_rejected(value: str) -> None:
    assert scan_value({"plan_id": value})
    assert scan_value({"blob_hash": value})


@pytest.mark.parametrize("value", [PLAN_ID, BLOB_ID])
@pytest.mark.parametrize("key", ["token", "setupToken", "secret", "contentBase64"])
def test_fixture_hashes_do_not_override_sensitive_key_rules(key: str, value: str) -> None:
    assert scan_value({key: value})


@pytest.mark.parametrize("prefix", ["sk-", "ghp_", "-----BEGIN ", "/Users/example/"])
def test_other_sensitive_checks_still_apply(prefix: str) -> None:
    assert scan_value({"note": prefix + PLAN_ID})


def test_public_removal_preimages_reproduce_exact_allowed_digests() -> None:
    path = PLAN_PATH.with_name("removal.json")
    rows = json.loads(path.read_text())
    assert len(rows) == 4
    for row in rows:
        digest = hashlib.sha256(row["preimage"].encode()).hexdigest()
        assert digest == row["sha256"]
        assert scan_value({"targetIdentityDigests": {"example": digest}}) == []
        assert scan_value({"targetKeyDigests": {"example": [digest]}}) == []
        assert scan_value({"token": digest})
        changed = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        assert scan_value({"targetIdentityDigests": {"example": changed}})


def test_public_write_plan_preimage_is_exact_and_keeps_sensitive_guards() -> None:
    record = json.loads(PLAN_PATH.with_name("write-plan.json").read_text())
    raw = json.dumps(record["unsigned"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert digest == record["planId"] == "e3437967395b7d0201428e58736e4616a7b51457a28f4746feb9ff39199eaebe"
    assert scan_value({"plan_id": digest}) == []
    for key in ("token", "setupToken", "secret", "contentBase64"):
        assert scan_value({key: digest})
    assert scan_value({"plan_id": digest[:-1] + "0"})
