"""Read projection and credential admission for the reference slice."""

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.shell.impl.request_http.catalog import build_view


def test_agent_reference_read_projection_and_write_exclusion():
    entries = [dict(row, declared=True, state="available", callable=True)
               for row in daemon_catalog()["methods"]]
    view = build_view(
        {"version": "test", "nodeId": "node", "owner": "owner", "epoch": "test", "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "test",
         "packageVersion": "test", "methods": entries},
        socket_path="/tmp/reference-test.sock", local_entries=entries,
    )
    assert {"agent.list", "agent.secret.list", "agent.provider.default"} <= set(view.routes)
    route = view.routes["agent.provider.default"]
    assert route.mutates is False
    assert route.retry == "safe"
    validator = Draft202012Validator(route.params_schema)
    assert validator.is_valid({})
    for params in ({"provider": "claude"}, {"provider": None}, {"clear": True},
                   {"clear": "yes"}, {"clear": False}, {"clear": []}, {"setupToken": "x"}):
        assert not validator.is_valid(params)
    assert METHOD_REGISTRY.get("agent.provider.default").spec.mutates
    for name in ("agent.provider.login", "agent.provider.logout"):
        assert not METHOD_REGISTRY.get(name).spec.http_exposed
        assert name not in view.routes
        assert f"/daemon/request/{name}" not in daemon_openapi()["paths"]


@pytest.mark.parametrize("method", ["agent.list", "agent.secret.list", "agent.provider.default",
                                    "agent.provider.login", "agent.provider.logout"])
def test_agent_error_contract_explicitly_declares_absent_data(method):
    for error in METHOD_REGISTRY.get(method).spec.errors:
        assert error.data_schema is not None or "no data" in error.description.lower()
