"""Batch 6: org.acl.* MethodSpec declarations (no GUI surface)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from pytest import MonkeyPatch

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import ipc_errors

_ACL_METHODS = ("org.acl.grant", "org.acl.ls", "org.acl.revoke")

_PARAMS = {
    "org.acl.grant": {
        "org": "group-x",
        "agent": "agent:owner:node:worker",
        "from": "user:owner",
    },
    "org.acl.ls": {"org": "group-x", "from": "user:owner"},
    "org.acl.revoke": {
        "org": "group-x",
        "agent": "agent:owner:node:worker",
        "from": "user:owner",
    },
}

_RESULTS = {
    "org.acl.grant": {
        "org": "group-x",
        "agent": "agent:owner:node:worker",
        "grantedAtMs": 1,
        "untilMs": None,
    },
    "org.acl.ls": {"org": "group-x", "grants": []},
    "org.acl.revoke": {
        "org": "group-x",
        "agent": "agent:owner:node:worker",
        "revoked": False,
    },
}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _spec(name: str):
    return next(spec for spec, *_handler in ORG_BINDINGS if spec.name == name)


def test_acl_profiles_are_ipc_only() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert set(_ACL_METHODS) <= set(registry.names)
    for name in _ACL_METHODS:
        spec = _spec(name)
        assert spec.http_exposed is False
        assert spec.http_binding is None
        assert spec.audience == ("local-operator",)
        if name == "org.acl.ls":
            assert (spec.mutates, spec.retry) == (False, "safe")
        else:
            assert (spec.mutates, spec.retry) == (True, "never")


def test_acl_params_accept_the_mcp_session_envelope() -> None:
    for name in _ACL_METHODS:
        schema = _plain(_spec(name).params_schema)
        params = {
            **_PARAMS[name],
            "actor": "agent:owner:node:deputy",
            "sessionRef": "mcp-session",
        }
        errors = list(Draft202012Validator(schema).iter_errors(params))
        assert not errors, (name, errors)


def test_acl_forbidden_declares_both_real_data_variants() -> None:
    error = next(
        error
        for error in _spec("org.acl.grant").errors
        if error.code == ipc_errors.ORG_ACL_GRANT_FORBIDDEN
    )
    assert error.data_schema is not None
    validator = Draft202012Validator(_plain(error.data_schema))
    for data in (
        {"method": "org.acl.ls", "caller": "agent:owner:node:worker"},
        {"org": "group-x", "reason": "not-owner-node"},
    ):
        errors = list(validator.iter_errors(data))
        assert not errors, (data, errors)


def test_acl_handlers_forward_with_the_trusted_origin(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], str | None]] = []
    entrypoints = {
        "org.acl.grant": "ipc_org_acl_grant",
        "org.acl.ls": "ipc_org_acl_ls",
        "org.acl.revoke": "ipc_org_acl_revoke",
    }

    from hyprial.daemon.impl.org.network import ipc as org_ipc

    for name in _ACL_METHODS:
        def recorder(
            app: Any,
            params: dict[str, Any],
            *,
            trusted_origin: str | None = None,
            name: str = name,
        ) -> dict[str, Any]:
            calls.append((name, dict(params), trusted_origin))
            return _RESULTS[name]

        # monkeypatch restores the real ACL entrypoints after this test; a bare
        # setattr would leak the recorder into the next test in the same process.
        monkeypatch.setattr(org_ipc, entrypoints[name], recorder)
        result = MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
            object(), name, _PARAMS[name], trusted_message_origin="ipc"
        )
        assert result == _RESULTS[name]
        assert calls[-1] == (name, _PARAMS[name], "ipc")


def test_acl_results_match_their_schemas() -> None:
    for name in _ACL_METHODS:
        errors = list(
            Draft202012Validator(_plain(_spec(name).result_schema)).iter_errors(
                _RESULTS[name]
            )
        )
        assert not errors, (name, errors)


def test_acl_errors_keep_the_real_data_schemas() -> None:
    for name in _ACL_METHODS:
        codes = {error.code for error in _spec(name).errors}
        assert ipc_errors.INVALID_ARGUMENT in codes
        assert "ORG_ACL_GRANT_FORBIDDEN" in codes
    grant = {error.code for error in _spec("org.acl.grant").errors}
    assert "ORG_ACL_GRANT_NOT_OWNED" in grant
