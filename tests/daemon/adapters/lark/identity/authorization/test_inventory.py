"""Only structurally valid tenant observations establish authorization."""

import pytest

from hyprial.daemon.impl.adapters.lark.credentials.authorization.inventory import scope_inventory
from tests.daemon.adapters.lark.identity.authorization.test_service import home as home


@pytest.mark.parametrize("response", [
    None, {}, {"code": False}, {"code": 0}, {"code": 0, "data": {}},
    {"code": 0, "data": {"scopes": {}}},
    {"code": 0, "data": {"scopes": [None]}},
    {"code": 0, "data": {"scopes": [{"scope_name": "im:message", "grant_status": True}]}},
    {"code": 0, "data": {"scopes": [{"scope_name": "im:message", "grant_status": 99}]}},
])
def test_malformed_inventory_remains_unconfirmed(home, tmp_path, monkeypatch, response):
    class Client:
        def list_scopes(self):
            return response
    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.authorization.inventory.configured_scope_client",
                        lambda *_: ("cli_test", Client()))
    result = scope_inventory(home, tmp_path, "test")
    assert result["authorization"] == "unconfirmed"


def test_valid_empty_inventory_is_unauthorized_and_user_scopes_do_not_grant(home, tmp_path, monkeypatch):
    class Client:
        def list_scopes(self):
            return {"code": 0, "data": {"scopes": [
                {"scope_name": "im:message", "grant_status": 1, "scope_type": "user"},
            ]}}
    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.authorization.inventory.configured_scope_client",
                        lambda *_: ("cli_test", Client()))
    result = scope_inventory(home, tmp_path, "test")
    assert result["authorization"] == "unauthorized"
    assert result["capabilities"] == []
    assert "message-send" in result["missingCapabilities"]


def test_missing_configuration_and_unreadable_configuration_are_distinct(home, tmp_path):
    assert scope_inventory(home, tmp_path, "missing")["authorization"] == "unconfigured"
    (home / "secrets" / "lark-test.json").write_text("not-json-should-not-leak")
    result = scope_inventory(home, tmp_path, "test")
    assert result["authorization"] == "unconfirmed"
    assert result["reason"] == "CONFIGURATION_UNAVAILABLE"
    assert "not-json" not in str(result)


@pytest.mark.parametrize("implicit_type", [None, "omitted"])
def test_conflicting_implicit_and_explicit_tenant_grants_are_unconfirmed(home, tmp_path, monkeypatch, implicit_type):
    implicit = {"scope_name": "im:message", "grant_status": 1}
    if implicit_type is None:
        implicit["scope_type"] = None

    class Client:
        def list_scopes(self):
            return {"code": 0, "data": {"scopes": [
                {"scope_name": "im:message", "scope_type": "tenant", "grant_status": 0},
                implicit,
                {"scope_name": "im:message.group_at_msg:readonly", "grant_status": 1},
            ]}}

    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.authorization.inventory.configured_scope_client",
                        lambda *_: ("cli_test", Client()))
    result = scope_inventory(home, tmp_path, "test")
    assert result["authorization"] == "unconfirmed"
    assert result["reason"] == "INVALID_SCOPE_INVENTORY"
