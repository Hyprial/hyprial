"""The exact 39-method example increment, including refusal-only declarations.

Serving the cases is covered by gui/tests/contract-mock (conformance and
chat-world tests) against the same projection.
"""

from __future__ import annotations

import json

import pytest
from contract_mock import load_contract

from hyprial.daemon import daemon_openapi

TARGETS = frozenset(
    "hosts top.snapshot org.delete org.execute org.leave "
    "org.network org.publish org.remove org.repair orgfs.spaces orgfs.resolve "
    "orgfs.ls orgfs.stat orgfs.read orgfs.read_at orgfs.stat_at orgfs.history "
    "orgfs.status orgfs.trash orgfs.watch orgfs.members orgfs.purge_status "
    "orgfs.create orgfs.write orgfs.export orgfs.import orgfs.mkdir orgfs.move "
    "orgfs.remove orgfs.restore orgfs.purge_plan orgfs.purge orgfs.space_purge "
    "orgfs.unban orgfs.serve orgfs.checkout orgfs.invite orgfs.remove_member "
    "orgfs.join".split()
)


@pytest.fixture()
def projection(tmp_path):
    document = daemon_openapi()
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return document, load_contract(path)


def test_exact_39_have_same_source_mock_cases(projection):
    document, contract = projection
    assert len(TARGETS) == 39
    assert TARGETS <= contract.methods.keys(), sorted(TARGETS - contract.methods.keys())
    catalog = {entry["name"]: entry for entry in document["x-hyprial-methods"]}
    for method in sorted(TARGETS):
        entry = catalog[method]
        operation = document["paths"][f"/daemon/request/{method}"]["post"]
        assert entry["httpExposed"] is True
        assert entry["x-hyprial-examples"] == operation["x-hyprial-examples"]
        assert {case["kind"] for case in operation["x-hyprial-examples"]} >= {
            "invalid-params"
        }
    for method in ("org.acl.grant", "org.acl.ls", "org.acl.revoke"):
        assert not catalog[method]["httpExposed"]
        assert f"/daemon/request/{method}" not in document["paths"]
        assert method not in contract.methods


def test_delete_has_real_success_and_refusal_cases(projection):
    _, contract = projection
    method = contract.methods["org.delete"]
    assert {case["name"] for case in method.cases_for("success")} == {
        "preview",
        "confirmed-complete",
    }
    refusal = next(
        case for case in method.examples
        if case.get("error", {}).get("code") == "ORG_ALREADY_DELETED"
    )
    assert refusal["kind"] == "domain-error"
    assert contract.case("org.delete", refusal["name"]) is refusal
