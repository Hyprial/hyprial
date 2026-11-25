"""Batch 3: read-only orgfs.* MethodSpec declarations.

Every handler forwards to ``DaemonApplication._handle_orgfs``; these tests pin
the read profiles, forwarding and the real result shapes captured from an
isolated daemon.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import ipc_errors

_READ_METHODS = (
    "orgfs.spaces",
    "orgfs.resolve",
    "orgfs.ls",
    "orgfs.stat",
    "orgfs.read",
    "orgfs.read_at",
    "orgfs.stat_at",
    "orgfs.history",
    "orgfs.status",
    "orgfs.trash",
    "orgfs.watch",
    "orgfs.members",
    "orgfs.purge_status",
)

_PARAMS = {
    "orgfs.spaces": {},
    "orgfs.resolve": {"spaceId": "space-x", "path": "docs/a.md"},
    "orgfs.ls": {"spaceId": "space-x", "path": "docs", "recursive": True},
    "orgfs.stat": {"spaceId": "space-x", "path": "docs/a.md"},
    "orgfs.read": {"spaceId": "space-x", "path": "docs/a.md"},
    "orgfs.read_at": {"spaceId": "space-x", "node": "id:n1", "version": "v1"},
    "orgfs.stat_at": {"spaceId": "space-x", "node": "id:n1", "version": "v1"},
    "orgfs.history": {"spaceId": "space-x", "path": "docs/a.md"},
    "orgfs.status": {"spaceId": "space-x"},
    "orgfs.trash": {"spaceId": "space-x"},
    "orgfs.watch": {"spaceId": "space-x", "glob": "*.md"},
    "orgfs.members": {"spaceId": "space-x"},
    "orgfs.purge_status": {"spaceId": "space-x", "planId": "plan-1"},
}

_NODE = {
    "space_id": "space-x",
    "node_id": "node-1",
    "kind": "doc",
    "name": "a.md",
    "path": "docs/a.md",
    "size": 5,
    "blob_hash": None,
    "doc_id": "doc-1",
    "version": "frontier-2",
    "modified_by": "user:owner",
    "modified_via": None,
    "name_conflict": False,
    "deleted": False,
    "contentState": "arrived",
    "uri": "orgfs:owner:space-x:node-1",
}

_RESULTS = {
    "orgfs.spaces": {
        "spaces": [
            {
                "space_id": "space-x",
                "name": "team",
                "owner": "user:owner",
                "created_at": "2026-11-02T10:00:00Z",
            }
        ]
    },
    "orgfs.resolve": {"nodes": [_NODE]},
    "orgfs.ls": {"nodes": [_NODE]},
    "orgfs.stat": _NODE,
    "orgfs.read": {"node": _NODE, "text": "hello", "version": "frontier-2"},
    "orgfs.read_at": {"contentB64": "aGVsbG8="},
    "orgfs.stat_at": _NODE,
    "orgfs.history": {
        "history": [
            {
                "node": {
                    **{key: value for key, value in _NODE.items() if key != "contentState"},
                    "content_state": "arrived",
                },
                "changed": "created",
                "author": "user:owner",
                "actor": None,
                "at": "2026-11-02T10:00:00Z",
            }
        ]
    },
    "orgfs.status": {
        "space_id": "space-x",
        "unconfirmed_commits": 0,
        "durable_holders_seen": [],
        "holders_online": [],
        "parked_commits": [],
    },
    "orgfs.trash": {"nodes": []},
    "orgfs.watch": {
        "events": [
            {
                "spaceId": "space-x",
                "kind": "created",
                "node": _NODE,
                "oldPath": None,
            }
        ]
    },
    "orgfs.members": {
        "members": [
            {
                "space_id": "space-x",
                "user": "user:owner",
                "mode": "rw",
                "added_by": "user:owner",
                "added_at": "2026-11-02T10:00:00Z",
            }
        ]
    },
    "orgfs.purge_status": {
        "plan_id": "plan-1",
        "acknowledged": [],
        "pending": [],
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


def test_batch_three_profiles_are_read_only() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert set(_READ_METHODS) <= set(registry.names)
    for name in _READ_METHODS:
        spec = _spec(name)
        assert (spec.mutates, spec.retry, spec.confirmation) == (
            False,
            "safe",
            "none",
        ), name
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.audience == ("local-operator",)
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "safe"


@pytest.mark.parametrize("method", _READ_METHODS)
def test_each_read_handler_forwards_to_the_orgfs_bridge(method: str) -> None:
    calls: list[tuple[str, dict[str, Any], str | None]] = []

    class App:
        def _handle_orgfs(
            self, name: str, params: dict[str, Any], *, trusted_origin: str | None
        ) -> dict[str, Any]:
            calls.append((name, dict(params), trusted_origin))
            return _RESULTS[method]

    result = MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
        App(), method, _PARAMS[method], trusted_message_origin="ipc"
    )
    assert result == _RESULTS[method]
    assert calls == [(method, _PARAMS[method], "ipc")]


@pytest.mark.parametrize("method", _READ_METHODS)
def test_each_real_result_shape_matches_its_schema(method: str) -> None:
    schema = _plain(_spec(method).result_schema)
    errors = list(Draft202012Validator(schema).iter_errors(_RESULTS[method]))
    assert not errors, (method, errors)


def test_read_rejects_an_undeclared_extra_field() -> None:
    schema = _plain(_spec("orgfs.read").result_schema)
    result = {**_RESULTS["orgfs.read"], "unexpected": True}
    assert list(Draft202012Validator(schema).iter_errors(result))


def test_read_at_requires_a_version() -> None:
    schema = _plain(_spec("orgfs.read_at").params_schema)
    assert list(
        Draft202012Validator(schema).iter_errors({"spaceId": "space-x", "node": "id:n1"})
    )


def test_orgfs_ls_recursive_is_an_optional_boolean() -> None:
    schema = _plain(_spec("orgfs.ls").params_schema)
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors({"spaceId": "space-x"}))
    assert not list(
        validator.iter_errors({"spaceId": "space-x", "recursive": True})
    )
    assert list(
        validator.iter_errors({"spaceId": "space-x", "recursive": "yes"})
    )


def test_purge_status_requires_a_plan_id() -> None:
    schema = _plain(_spec("orgfs.purge_status").params_schema)
    assert list(Draft202012Validator(schema).iter_errors({"spaceId": "space-x"}))


def test_batch_three_declarations_include_the_shared_argument_code() -> None:
    for name in _READ_METHODS:
        codes = {error.code for error in _spec(name).errors}
        assert ipc_errors.INVALID_ARGUMENT in codes, name
        assert "invalid-argument" not in codes, name


def test_every_read_accepts_the_mcp_session_envelope() -> None:
    """MCP adds actor/sessionRef to every tool call before the registry sees it."""

    for name in _READ_METHODS:
        schema = _plain(_spec(name).params_schema)
        params = {
            **_PARAMS[name],
            "actor": "agent:owner:node:worker",
            "sessionRef": "mcp-session",
        }
        errors = list(Draft202012Validator(schema).iter_errors(params))
        assert not errors, (name, errors)
        assert schema["properties"]["actor"]["x-hyprial-http-forbidden"] is True
        assert schema["properties"]["sessionRef"]["x-hyprial-http-forbidden"] is True


def test_mcp_session_envelope_reaches_the_bridge_unchanged() -> None:
    """The registry must not strip the fields the org-context fence consumes."""

    calls: list[tuple[str, dict[str, Any]]] = []

    class App:
        def _handle_orgfs(
            self, name: str, params: dict[str, Any], *, trusted_origin: str | None
        ) -> dict[str, Any]:
            calls.append((name, dict(params)))
            return {"spaces": []}

    params = {
        "actor": "agent:owner:node:worker",
        "sessionRef": "mcp-session",
    }
    MethodRegistry.from_bindings(ORG_BINDINGS).invoke(App(), "orgfs.spaces", params)
    assert calls == [("orgfs.spaces", params)]


def test_org_context_reads_declare_the_real_refusal_and_session_codes() -> None:
    """Only the six methods that call _enforce_org_context claim these codes."""

    org_context_methods = {
        "orgfs.history",
        "orgfs.ls",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.stat",
        "orgfs.stat_at",
    }
    for name in org_context_methods:
        errors = {error.code: error for error in _spec(name).errors}
        assert {
            ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED,
            ipc_errors.STALE_SESSION,
            ipc_errors.SESSION_SUPERSEDED,
        } <= set(errors), name
        data = errors[ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED].data_schema
        assert data is not None
        assert not list(
            Draft202012Validator(_plain(data)).iter_errors(
                {
                    "caller": "agent:owner:node:visitor",
                    "capability": "org-context",
                    "entryPoint": "orgfs.read",
                    "resource": "accepted",
                    "reason": "no grant",
                }
            )
        )
        assert (
            errors[ipc_errors.STALE_SESSION].data_schema is None
            and errors[ipc_errors.SESSION_SUPERSEDED].data_schema is None
        )

    for name in set(_READ_METHODS) - org_context_methods:
        codes = {error.code for error in _spec(name).errors}
        assert ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED not in codes, name
