from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/agent_home_p20"
SNAPSHOT = (
    ROOT
    / "tests/fixtures/agent_home_evidence/agent-home-p2-p20-20260921/probe-snapshot.json"
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "probe_agent_home_p20", ROOT / "scripts/archived/probe_agent_home_p20.py"
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
probe = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = probe
MODULE_SPEC.loader.exec_module(probe)


def snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT.read_text())


def test_native_fixtures_are_parseable_nonsecret_and_complete() -> None:
    result = probe.verify_fixtures(FIXTURES)

    assert result["state"] == "PASS"
    assert result["missing"] == []
    assert result["parse_errors"] == []
    assert result["secret_findings"] == []
    assert result["sentinel_shapes_exact"] is True
    assert result["manifest"]["file_count"] == 26
    assert result["manifest"]["tree_sha256"] == (
        "351aa5b0315b599c7d238c1ed1bd8caad1090a704c808e8e9b5b37e194725fd3"
    )


def test_frozen_snapshot_records_real_schema_and_honest_unknowns() -> None:
    result = snapshot()

    assert result["schema_version"] == 1
    assert result["claude"]["version"] == "2.1.278 (Claude Code)"
    assert result["claude"]["config_root"]["state"] == "PASS"
    assert result["claude"]["effective_settings"]["state"] == "NOT_RUN"
    assert result["claude"]["auth_refresh"]["state"] == "NOT_RUN"

    assert result["claude_sdk"]["schema"]["version"] == "0.2.125"
    assert set(result["claude_sdk"]["schema"]["setting_sources"].split("'")) >= {
        "user",
        "project",
        "local",
    }

    assert result["codex"]["version"] == "codex-cli 0.153.4"
    schema = result["codex"]["schema_generation"]
    assert schema["state"] == "PASS"
    assert schema["manifest"]["file_count"] == 304
    assert schema["manifest"]["total_bytes"] > 3_000_000
    assert schema["manifest"]["tree_sha256"] == (
        "7b008240c5e201a9cca7651f4c0b4d37c7fbbb66431a8f6bac83a6befb031583"
    )
    assert {"initialize", "config/read", "thread/start"} <= set(
        schema["methods"]["ClientRequest"]
    )
    assert result["codex"]["config_discovery"]["state"] == "PASS"
    assert result["codex"]["config_discovery"]["effective_model"] == (
        "p20-codex-project-sentinel"
    )
    assert result["codex"]["config_discovery"]["layer_types"] == [
        "project",
        "user",
        "system",
    ]
    assert result["codex"]["auth_refresh"]["state"] == "NOT_RUN"

    assert result["pi"]["version"] == "0.85.1"
    assert result["pi"]["default_root_help"]["state"] == "PASS"
    assert result["pi"]["default_root_help"]["written_files"] == [
        "home/.pi/agent/auth.json",
        "home/.pi/agent/models-store.json",
    ]
    assert result["pi"]["rpc"]["state"] == "PASS"
    assert result["pi"]["resource_discovery"]["state"] == "PASS"
    boundary = result["pi"]["resource_discovery"]["context_boundary"]
    assert boundary["state"] == "UNSUPPORTED"
    assert boundary["outside_repository_ancestor_loaded"] is True
    assert result["pi"]["package_artifacts"]["rpc_types"]["sha256"] == (
        "e968e5be01dc7ad9615f938ae867ef136fa495f13dcf169942e9f781a299d9eb"
    )
    assert result["pi"]["auth_refresh"]["state"] == "NOT_RUN"

    assert result["tools"]["service_identity"]["state"] == "NOT_RUN"
    assert result["tools"]["ssh_signing_identity"]["state"] == "NOT_RUN"


def test_contract_compare_ignores_capture_time_but_detects_schema_drift() -> None:
    expected = snapshot()
    same_contract = copy.deepcopy(expected)
    same_contract["captured_at_utc"] = "2099-01-01T00:00:00Z"
    same_contract["codex"]["commands"][0]["duration_ms"] += 1000

    assert probe.compare(expected, same_contract)["state"] == "PASS"

    drifted = copy.deepcopy(same_contract)
    drifted["codex"]["schema_generation"]["manifest"]["tree_sha256"] = (
        "0" * 64
    )
    assert probe.compare(expected, drifted)["state"] == "FAIL"


def test_contract_compare_detects_pi_boundary_regression() -> None:
    expected = snapshot()
    drifted = copy.deepcopy(expected)
    boundary = drifted["pi"]["resource_discovery"]["context_boundary"]
    boundary["state"] = "PASS"
    boundary["outside_repository_ancestor_loaded"] = False

    assert probe.compare(expected, drifted)["state"] == "FAIL"


def test_fixture_verifier_rejects_credential_shaped_material(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copied)
    bad = copied / "pi/native-root/accidental-secret.txt"
    bad.write_text(
        "-----BEGIN " + "OPENSSH " + "PRIVATE KEY-----\nnot-a-real-key\n"
    )

    result = probe.verify_fixtures(copied)

    assert result["state"] == "FAIL"
    assert result["secret_findings"] == [
        "pi/native-root/accidental-secret.txt"
    ]


def test_snapshot_contains_no_unredacted_sentinel() -> None:
    raw = SNAPSHOT.read_text()

    assert probe.SENTINEL not in raw
