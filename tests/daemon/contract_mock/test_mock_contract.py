"""Contract Mock example loading and validation (the served mock is JavaScript)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import contract_mock.checks as checks
from contract_mock import SCENARIOS, ContractError, check_contract, load_contract
from contract_mock.contract import DEFAULT_CONTRACT
from contract_mock.sensitive import SENTINEL, SensitiveMaterialError


PROVIDER_KEY_METHODS = (
    "agent.provider.allow", "agent.provider.revoke", "agent.provider.status",
    "user.provider.list", "user.provider.login", "user.provider.logout", "user.provider.rotate",
)


def _contract(path: Path):
    return load_contract(path)


@pytest.mark.parametrize("method", PROVIDER_KEY_METHODS)
def test_local_operator_provider_key_examples(method: str) -> None:
    document = json.loads(DEFAULT_CONTRACT.read_text(encoding="utf-8"))
    declaration = next(item for item in document["x-hyprial-methods"] if item["name"] == method)
    operation = document["paths"][f"/daemon/request/{method}"]["post"]
    assert declaration["audience"] == ["local-operator"]
    cases = operation.get("x-hyprial-examples", [])
    assert {"success", "invalid-params", "domain-error"} <= {case["kind"] for case in cases}
    assert cases == declaration["x-hyprial-examples"]
    failures = {case["error"]["code"] for case in cases if case["kind"] == "domain-error"}
    assert "CALLER_NOT_AUTHORIZED" in failures
    assert failures - {"CALLER_NOT_AUTHORIZED"}
    for case in cases:
        for field in ("secret", "replaceSecret"):
            if field in case["params"]:
                assert case["params"][field] == SENTINEL
        assert SENTINEL not in json.dumps(case.get("result", case.get("error")))


def test_contract_loads_the_business_projection(contract_file: Path) -> None:
    contract = _contract(contract_file)
    assert set(contract.methods) == {"demo.list", "demo.write"}
    assert contract.contract_version == "1"
    assert [
        scenario for scenario in SCENARIOS if contract.methods["demo.list"].cases_for(scenario)
    ] == ["success", "empty", "error"]
    assert contract.methods["demo.write"].gap_for("empty")


def test_a_case_is_found_by_name_and_an_unknown_one_is_refused(
    contract_file: Path,
) -> None:
    contract = _contract(contract_file)
    assert contract.case("demo.list", "all")["kind"] == "success"
    with pytest.raises(ContractError, match="no example case named"):
        contract.case("demo.list", "no-such-case")
    with pytest.raises(ContractError, match="unknown method"):
        contract.case("no.such", "all")


def test_the_synthetic_contract_passes_the_checks(contract_file: Path) -> None:
    check_contract(_contract(contract_file))


def test_sensitive_material_is_rejected_by_the_checks(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    operation["x-hyprial-examples"][0]["params"]["setupToken"] = "sk-real-secret"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(tampered)
    with pytest.raises(SensitiveMaterialError, match="synthetic sentinel"):
        check_contract(contract)


def test_check_contract_covers_the_real_projection(monkeypatch) -> None:
    contract = load_contract()
    built: list[str] = []
    original = checks.Envelope

    class Counting(original):
        def success(self, method, request_id, result):
            built.append(method)
            return super().success(method, request_id, result)

        def error(self, request_id, code, message, dispatch="not-dispatched", **kwargs):
            built.append(kwargs["method"])
            return super().error(request_id, code, message, dispatch, **kwargs)

    monkeypatch.setattr(checks, "Envelope", Counting)
    check_contract(contract)
    assert len(built) == sum(len(method.examples) for method in contract.methods.values())
    assert set(built) == set(contract.methods)


def test_no_product_import_is_used_by_the_mock(contract_file: Path) -> None:
    package = Path(__file__).resolve().parents[3] / "contract" / "daemon-request" / "contract_mock"
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "from hyprial" not in text, source
        assert "import hyprial" not in text, source




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


def test_the_real_registered_method_projection_loads_and_passes_the_checks(
    tmp_path: Path,
) -> None:
    from hyprial.daemon import daemon_openapi

    document = daemon_openapi()
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(path)
    expected_http_methods = {
        *PROVIDER_KEY_METHODS,
        "adapter.identities.list",
        "adapter.identities.sync",
        "adapter.register",
        "adapter.authorization.status",
        "adapter.authorization.begin",
        "adapter.authorization.operation",
        "adapter.authorization.cancel",
        "adapter.onboard.begin",
        "adapter.onboard.operation",
        "adapter.onboard.cancel",
        "adapter.routes.list",
        "adapter.routes.candidates",
        "adapter.routes.add",
        "adapter.routes.remove",
        "adapter.reload",
        "management.adapter.remove",
        "management.adapter.remove.preview",
        "adapter.start",
        "adapter.stop",
        "adapter.list",
        "adapter.status",
        "agent.cell.get",
        "agent.cell.update",
        "message.history",
        "message.query",
        "message.send",
        "message.send.key_lookup",
        "message.send.lookup",
        "targets",
        "progress.list",
        "agent.home-census",
        "agent.list",
        "agent.secret.list",
        "agent.secret.grant",
        "agent.secret.provider-write",
        "agent.secret.provider-list",
        "agent.secret.provider-configure",
        "agent.secret.provider-test",
        "agent.secret.revoke",
        "agent.create",
        "agent.destroy.preview",
        "agent.destroy",
        "agent.stop",
        "agent.start",
        "agent.provider.default",
        "agent.provider.auth.list",
        "agent.provider.login",
        "agent.provider.logout",
        "routine.list",
        "routine.status",
        "workflow.list",
        # The declared onboarding/auth methods are http_exposed and carry
        # examples now, so the real projection is mockable for them too.
        "onboarding.status",
        "onboarding.device.ensure",
        "onboarding.initialize",
        "onboarding.firstRun.begin",
        "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm",
        "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
        "auth.login.begin",
        "auth.logout",
        "auth.operation.cancel",
        "auth.operation.get",
        "auth.relogin",
        "org.create",
        "org.fetch",
        "org.invite",
        "org.join",
        "org.list",
        "org.invites.list",
        "hosts",
        "autoupdate.status",
        "daemon.status",
        "identity.users.list",
        "log.page",
        "upgrade.steps.status",
        "identity.users.add",
        "org.delete",
        "org.execute",
        "org.leave",
        "org.network",
        "org.publish",
        "org.remove",
        "org.repair",
        "orgfs.checkout",
        "orgfs.create",
        "orgfs.export",
        "orgfs.history",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.purge",
        "orgfs.purge_plan",
        "orgfs.purge_status",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.resolve",
        "orgfs.restore",
        "orgfs.serve",
        "orgfs.space_purge",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.stat_at",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.unban",
        "orgfs.watch",
        "orgfs.write",
        "top.snapshot",
        "agent.rename",
        # cell-model phase B first slice: HTTP-exposed conversation reads.
        "conversations.history",
        "conversations.list",
        "conversations.create",
        "conversations.get",
        "conversations.members.add",
        "conversations.members.remove",
        "conversations.post",
    }
    declarations = document["x-hyprial-methods"]
    http_with_examples = {
        entry["name"] for entry in declarations
        if entry["httpExposed"] and entry.get("x-hyprial-examples")
    }
    ipc_with_examples = {
        entry["name"] for entry in declarations
        if not entry["httpExposed"] and entry.get("x-hyprial-examples")
    }
    ipc_without_examples = {
        entry["name"] for entry in declarations
        if not entry["httpExposed"] and not entry.get("x-hyprial-examples")
    }
    assert http_with_examples == expected_http_methods
    assert set(contract.methods) == expected_http_methods | ipc_with_examples
    assert ipc_without_examples.isdisjoint(contract.methods)
    for name in ipc_with_examples | ipc_without_examples:
        assert f"/daemon/request/{name}" not in document["paths"]
    assert contract.skipped == {}
    assert contract.methods["agent.provider.login"].gap_for("empty")
    assert contract.methods["agent.provider.default"].cases_for("unconfigured")
    assert contract.methods["org.list"].cases_for("empty")
    assert contract.methods["org.fetch"].cases_for("empty")
    for method in sorted(contract.methods):
        assert contract.methods[method].cases_for("success"), method
    assert contract.methods["message.send"].echo == {"idempotencyKey": "operationId"}
    assert contract.methods["message.send.key_lookup"].echo == {"operationKey": "commandId"}
    assert contract.methods["message.send.lookup"].echo == {"idempotencyKey": "commandId"}
    assert contract.methods["conversations.post"].echo == {"idempotencyKey": "operationId"}
    check_contract(contract)


def test_echo_validation_rejects_unknown_param_or_result_fields(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    operation["x-hyprial-mock-echo"] = {"actor": "missingResultField"}
    bad_result = tmp_path / "bad-echo-result.json"
    bad_result.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="x-hyprial-mock-echo"):
        load_contract(bad_result)

    operation["x-hyprial-mock-echo"] = {"missingParam": "actor"}
    bad_param = tmp_path / "bad-echo-param.json"
    bad_param.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="x-hyprial-mock-echo"):
        load_contract(bad_param)


@pytest.mark.parametrize("echo", [None, {}], ids=["null", "empty"])
@pytest.mark.parametrize("location", ["path", "declaration"])
def test_echo_validation_rejects_explicit_null_and_empty_mapping(
    contract_file: Path, tmp_path: Path, echo: object, location: str
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    declaration = next(
        entry for entry in document["x-hyprial-methods"] if entry["name"] == "demo.write"
    )
    if location == "path":
        operation["x-hyprial-mock-echo"] = echo
    else:
        del operation["x-hyprial-examples"]
        declaration["httpExposed"] = False
        declaration["x-hyprial-mock-echo"] = echo
    bad_echo = tmp_path / f"bad-echo-{location}-{echo is None}.json"
    bad_echo.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="x-hyprial-mock-echo"):
        load_contract(bad_echo)


def test_echo_validation_accepts_missing_echo_on_path_and_declaration(
    contract_file: Path, tmp_path: Path
) -> None:
    document = json.loads(contract_file.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    declaration = next(
        entry for entry in document["x-hyprial-methods"] if entry["name"] == "demo.write"
    )
    operation.pop("x-hyprial-mock-echo", None)
    del operation["x-hyprial-examples"]
    declaration["httpExposed"] = False
    declaration.pop("x-hyprial-mock-echo", None)
    missing_echo = tmp_path / "missing-echo.json"
    missing_echo.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(missing_echo)
    assert contract.methods["demo.write"].echo == {}
