"""Contract Mock behavior: explicit scenarios, envelopes and fail-closed auth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_mock import ContractError, Envelope, MockService, load_contract
from contract_mock.sensitive import SensitiveMaterialError

TOKEN = "mock-test-token"


def _contract(path: Path):
    return load_contract(path)


def _service(
    path: Path,
    scenario: str = "success",
    **overrides,
) -> MockService:
    contract = _contract(path)
    return MockService(
        contract,
        Envelope(contract),
        scenario=scenario,
        token=TOKEN,
        **overrides,
    )


def _envelope_validator(contract, kind: str):
    from jsonschema import Draft202012Validator

    return Draft202012Validator(contract.envelope_schema["$defs"][kind])


def test_contract_loads_the_business_projection(contract_file: Path) -> None:
    contract = _contract(contract_file)
    assert set(contract.methods) == {"demo.list", "demo.write"}
    assert contract.digest.startswith("sha256:")
    assert contract.contract_version == "1"
    assert contract.methods["demo.list"].available_scenarios() == (
        "success",
        "empty",
        "error",
    )
    assert contract.methods["demo.write"].gap_for("empty")


def test_success_envelope_matches_the_pinned_schema(contract_file: Path) -> None:
    service = _service(contract_file)
    response = service.handle("demo.list", {"filter": None}, f"Bearer {TOKEN}")
    assert response.status == 200
    assert response.payload["ok"] is True
    assert response.payload["method"] == "demo.list"
    assert response.payload["dispatch"] == "dispatched"
    assert response.payload["result"] == {"ok": True, "items": ["alpha"]}
    _envelope_validator(service.contract, "success").validate(response.payload)


def test_empty_is_a_legal_success_and_unconfigured_is_explicit(
    contract_file: Path,
) -> None:
    empty = _service(contract_file, scenario="empty")
    response = empty.handle("demo.list", {"filter": "missing"}, f"Bearer {TOKEN}")
    assert response.status == 200
    assert response.payload["result"] == {"ok": True, "items": []}
    missing = empty.handle("demo.write", {"actor": "a", "setupToken": "example-not-a-credential"}, f"Bearer {TOKEN}")
    assert missing.status == 409
    assert missing.payload["code"] == "MOCK_SCENARIO_MISSING"
    assert "no legal empty result" in missing.payload["message"]


def test_domain_error_is_dispatched_with_the_transport_status(
    contract_file: Path,
) -> None:
    service = _service(contract_file, scenario="error")
    response = service.handle("demo.list", {"filter": None}, f"Bearer {TOKEN}")
    assert response.status == 503
    assert response.payload["ok"] is False
    assert response.payload["code"] == "DAEMON_NOT_READY"
    assert response.payload["dispatch"] == "dispatched"
    assert response.payload["data"] == {"reason": "restore"}
    _envelope_validator(service.contract, "error").validate(response.payload)


def test_invalid_params_are_refused_before_dispatch(contract_file: Path) -> None:
    service = _service(contract_file)
    response = service.handle("demo.list", {"filter": 5}, f"Bearer {TOKEN}")
    assert response.status == 400
    assert response.payload["code"] == "INVALID_ARGUMENT"
    assert response.payload["dispatch"] == "not-dispatched"
    assert response.payload["method"] == "demo.list"
    _envelope_validator(service.contract, "error").validate(response.payload)


def test_auth_is_required_and_never_self_reported(contract_file: Path) -> None:
    service = _service(contract_file)
    for header in (None, "Bearer wrong", TOKEN):
        response = service.handle("demo.list", {"filter": None}, header)
        assert response.status == 401
        assert response.payload["code"] == "HTTP_UNAUTHORIZED"
        assert response.payload["dispatch"] == "not-dispatched"


def test_unknown_method_is_a_404_without_dispatch(contract_file: Path) -> None:
    service = _service(contract_file)
    response = service.handle("demo.missing", {}, f"Bearer {TOKEN}")
    assert response.status == 404
    assert response.payload["code"] == "METHOD_NOT_FOUND"
    assert response.payload["dispatch"] == "not-dispatched"


def test_an_explicit_case_wins_over_the_global_scenario(
    contract_file: Path,
) -> None:
    service = _service(
        contract_file, scenario="success", method_cases={"demo.list": "daemon-restoring"}
    )
    response = service.handle("demo.list", {"filter": None}, f"Bearer {TOKEN}")
    assert response.status == 503
    assert response.payload["code"] == "DAEMON_NOT_READY"


def test_an_unavailable_explicit_scenario_fails_at_startup(
    contract_file: Path,
) -> None:
    with pytest.raises(ContractError, match="no legal empty result|no empty"):
        _service(contract_file, method_scenarios={"demo.write": "empty"})


def test_sensitive_material_is_rejected_before_serving(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    operation["x-hyprial-examples"][0]["params"]["setupToken"] = "sk-real-secret"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(tampered)
    with pytest.raises(SensitiveMaterialError, match="synthetic sentinel"):
        MockService(
            contract,
            Envelope(contract),
            scenario="success",
            token=TOKEN,
        )


def test_no_product_import_is_used_by_the_mock(contract_file: Path) -> None:
    package = Path(__file__).resolve().parents[3] / "contract" / "daemon-request" / "contract_mock"
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "from hyprial" not in text, source
        assert "import hyprial" not in text, source


def test_contract_metadata_reports_scenarios_and_gaps(contract_file: Path) -> None:
    service = _service(contract_file, scenario="empty")
    document = service.contract_document()
    assert document["mock"] is True
    assert document["scenario"] == "empty"
    assert document["methods"]["demo.list"]["scenarios"] == ["success", "empty", "error"]
    assert document["methods"]["demo.write"]["gaps"]["empty"]
    assert document["unsupportedForScenario"] == ["demo.write"]
    assert document["skipped"] == {}


def test_a_method_without_examples_is_skipped_not_faked(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.list"]["post"]
    del operation["x-hyprial-examples"]
    skipped = tmp_path / "one-skipped.json"
    skipped.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(skipped)
    assert set(contract.methods) == {"demo.write"}
    assert contract.skipped == {"demo.list": "no x-hyprial-examples"}


def test_examples_without_any_mockable_method_are_blocked(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    for operation in (
        entry["post"] for entry in document["paths"].values()
    ):
        del operation["x-hyprial-examples"]
    blocked = tmp_path / "no-examples.json"
    blocked.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="no mockable methods"):
        load_contract(blocked)


def test_the_mock_check_mode_is_explicit(contract_file: Path) -> None:
    from contract_mock.server import main

    exit_code = main(
        [
            "--openapi",
            str(contract_file),
            "--scenario",
            "success",
            "--token",
            TOKEN,
            "--check",
        ]
    )
    assert exit_code == 0


def test_a_schema_valid_semantic_error_is_served_as_dispatched(
    contract_file: Path,
) -> None:
    service = _service(
        contract_file,
        scenario="error",
        method_cases={"demo.list": "unknown-filter"},
    )
    response = service.handle(
        "demo.list", {"filter": "unknown"}, f"Bearer {TOKEN}"
    )
    assert response.status == 400
    assert response.payload["code"] == "INVALID_ARGUMENT"
    assert response.payload["dispatch"] == "dispatched"
    _envelope_validator(service.contract, "error").validate(response.payload)


def test_non_object_schema_negative_survives_projection_and_pre_dispatch(
    contract_file: Path,
) -> None:
    service = _service(contract_file)
    cases = {case["name"]: case for case in service.contract.methods["demo.list"].examples}
    assert cases["non-object-body"]["params"] == []
    response = service.handle("demo.list", [], f"Bearer {TOKEN}")
    assert response.status == 400
    assert response.payload["code"] == "INVALID_ARGUMENT"
    assert response.payload["dispatch"] == "not-dispatched"


def test_the_real_five_method_projection_loads_and_serves_success(
    tmp_path: Path,
) -> None:
    from hyprial.daemon import daemon_openapi

    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(daemon_openapi()), encoding="utf-8")
    contract = load_contract(path)
    assert set(contract.methods) == {
        "agent.list",
        "agent.secret.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
    }
    assert contract.skipped == {
        "link.list": "no x-hyprial-examples",
        "link.stats": "no x-hyprial-examples",
    }
    assert contract.methods["agent.provider.login"].gap_for("empty")
    assert "unconfigured" in contract.methods["agent.provider.default"].available_scenarios()
    service = MockService(
        contract, Envelope(contract), scenario="success", token=TOKEN
    )
    for method in sorted(contract.methods):
        case = service.selected_case(method)
        assert case is not None, method
        response = service.handle(method, case["params"], f"Bearer {TOKEN}")
        assert response.status == 200, method
        assert response.payload["ok"] is True, method
