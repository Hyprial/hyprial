# Decisions (phase D, package D4b, 2026-11-25):
# - Design §1.2: ``orgfs.remove`` takes an optional boolean ``permanent``;
#   ``orgfs.restore`` has no ``version`` and refuses one (closed params).
# - ``orgfs.checkout`` declares ``ORGFS_LAYOUT_UNSUPPORTED`` with its data
#   schema now that the bridge passes the refusal through (S report §9-3).
# - The journal-only codes without a producer are no longer declared.
"""Batch 4: write ``orgfs.*`` MethodSpec declarations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import ipc_errors

_WRITE_METHODS = (
    "orgfs.create",
    "orgfs.write",
    "orgfs.export",
    "orgfs.import",
    "orgfs.mkdir",
    "orgfs.move",
    "orgfs.remove",
    "orgfs.restore",
    "orgfs.space_purge",
    "orgfs.checkout",
    "orgfs.invite",
    "orgfs.remove_member",
    "orgfs.join",
)

_PARAMS = {
    "orgfs.create": {"name": "team"},
    "orgfs.write": {"spaceId": "space-x", "path": "docs/a.md", "text": "hi"},
    "orgfs.export": {
        "spaceId": "space-x",
        "path": "docs/a.md",
        "destination": "/tmp/out.md",
        "from": "user:owner",
    },
    "orgfs.import": {
        "spaceId": "space-x",
        "path": "docs/a.md",
        "source": "/tmp/in.md",
        "from": "user:owner",
    },
    "orgfs.mkdir": {"spaceId": "space-x", "path": "docs"},
    "orgfs.move": {
        "spaceId": "space-x",
        "sourcePath": "docs/a.md",
        "destinationPath": "docs/b.md",
    },
    "orgfs.remove": {"spaceId": "space-x", "path": "docs/a.md"},
    "orgfs.restore": {"spaceId": "space-x", "node": "id:n1"},
    "orgfs.space_purge": {"spaceId": "space-x"},
    "orgfs.checkout": {"spaceId": "space-x", "enabled": True},
    "orgfs.invite": {"spaceId": "space-x", "user": "user:bob", "mode": "ro"},
    "orgfs.remove_member": {"spaceId": "space-x", "user": "user:bob"},
    "orgfs.join": {"spaceId": "space-x"},
}

_SPACE = {
    "space_id": "space-x",
    "name": "team",
    "owner": "user:owner",
    "created_at": "2026-11-02T10:00:00Z",
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
_MEMBER = {
    "space_id": "space-x",
    "user": "user:bob",
    "mode": "ro",
    "added_by": "user:owner",
    "added_at": "2026-11-02T10:00:00Z",
}
_RESULTS = {
    "orgfs.create": _SPACE,
    "orgfs.write": _NODE,
    "orgfs.export": _NODE,
    "orgfs.import": _NODE,
    "orgfs.mkdir": _NODE,
    "orgfs.move": _NODE,
    "orgfs.remove": {"ok": True},
    "orgfs.restore": _NODE,
    "orgfs.space_purge": {
        "spaceId": "space-x",
        "purged": True,
        "localOnly": True,
    },
    "orgfs.checkout": {
        "spaceId": "space-x",
        "enabled": True,
        "path": "/state/checkout",
    },
    "orgfs.invite": _MEMBER,
    "orgfs.remove_member": {"ok": True},
    "orgfs.join": _SPACE,
}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _spec(name: str):
    return next(spec for spec, *_handler in ORG_BINDINGS if spec.name == name)


def test_batch_four_profiles_are_writes() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert set(_WRITE_METHODS) <= set(registry.names)
    for name in _WRITE_METHODS:
        spec = _spec(name)
        assert (spec.mutates, spec.retry, spec.confirmation) == (
            True,
            "never",
            "none",
        ), name
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.audience == ("local-operator",)
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "never"


def test_export_and_import_use_the_trusted_local_owner_selector() -> None:
    for name in ("orgfs.export", "orgfs.import"):
        binding = _spec(name).http_binding
        assert binding is not None
        assert binding.identity.mode == "local-owner-selector"
        assert binding.identity.param == "from"


@pytest.mark.parametrize("method", _WRITE_METHODS)
def test_each_write_handler_forwards_to_the_orgfs_bridge(method: str) -> None:
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


@pytest.mark.parametrize("method", _WRITE_METHODS)
def test_each_real_write_result_shape_matches_its_schema(method: str) -> None:
    schema = _plain(_spec(method).result_schema)
    errors = list(Draft202012Validator(schema).iter_errors(_RESULTS[method]))
    assert not errors, (method, errors)


def test_write_requires_exactly_one_payload() -> None:
    schema = _plain(_spec("orgfs.write").params_schema)
    base = {"spaceId": "space-x", "path": "docs/a.md"}
    assert not list(Draft202012Validator(schema).iter_errors({**base, "text": "hi"}))
    assert list(Draft202012Validator(schema).iter_errors(base))
    assert list(
        Draft202012Validator(schema).iter_errors(
            {**base, "text": "hi", "contentB64": "aGk="}
        )
    )


def test_export_and_import_require_the_operator_selector() -> None:
    for name in ("orgfs.export", "orgfs.import"):
        schema = _plain(_spec(name).params_schema)
        params = dict(_PARAMS[name])
        params.pop("from")
        assert list(Draft202012Validator(schema).iter_errors(params))


def test_batch_four_declarations_are_single_spelling() -> None:
    for name in _WRITE_METHODS:
        codes = {error.code for error in _spec(name).errors}
        assert ipc_errors.INVALID_ARGUMENT in codes, name
        assert "invalid-argument" not in codes, name


def test_every_write_accepts_the_cli_from_claim() -> None:
    """The owner terminal and worker claim stay on the IPC/MCP wire."""

    for name in _WRITE_METHODS:
        schema = _plain(_spec(name).params_schema)
        params = {**_PARAMS[name], "from": "user:owner"}
        errors = list(Draft202012Validator(schema).iter_errors(params))
        assert not errors, (name, errors)
        assert schema["properties"]["from"]["x-hyprial-http-forbidden"] is True


def test_every_write_accepts_the_mcp_session_envelope() -> None:
    """MCP adds actor/sessionRef before the registry sees any orgfs write."""

    for name in _WRITE_METHODS:
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


def test_orgfs_write_exposes_create_only_for_safe_new_files() -> None:
    schema = _plain(_spec("orgfs.write").params_schema)
    assert schema["properties"]["createOnly"] == {"type": "boolean"}
    params = {**_PARAMS["orgfs.write"], "createOnly": True}
    assert not list(Draft202012Validator(schema).iter_errors(params))
    rejected = {**_PARAMS["orgfs.write"], "createOnly": "true"}
    assert list(Draft202012Validator(schema).iter_errors(rejected))
    binary_create = {
        "spaceId": "space-x",
        "path": "keep.bin",
        "contentB64": "aGk=",
        "createOnly": True,
    }
    assert list(Draft202012Validator(schema).iter_errors(binary_create))


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        ({"text": ""}, True),
        ({"text": "hi"}, True),
        ({"text": "hi", "createOnly": True}, True),
        ({"text": "hi", "createOnly": False}, True),
        ({"contentB64": "aGk="}, True),
        ({"contentB64": "aGk=", "createOnly": False}, True),
        ({"contentB64": "aGk=", "createOnly": True}, False),
        ({"text": "hi", "contentB64": "aGk="}, False),
        ({"text": "hi", "contentB64": "aGk=", "createOnly": False}, False),
        ({"text": "hi", "contentB64": "aGk=", "createOnly": True}, False),
        ({}, False),
    ],
)
def test_orgfs_write_payload_matrix(
    payload: dict[str, Any], accepted: bool
) -> None:
    """Text and binary payloads are mutually exclusive at the schema level."""

    schema = _plain(_spec("orgfs.write").params_schema)
    params = {"spaceId": "space-x", "path": "docs/a.md", **payload}
    errors = list(Draft202012Validator(schema).iter_errors(params))
    assert (not errors) is accepted, (payload, errors)


def test_export_declares_org_context_and_both_writes_declare_session_errors() -> None:
    export_errors = {error.code: error for error in _spec("orgfs.export").errors}
    org_context = export_errors[ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED]
    assert org_context.data_schema is not None
    assert not list(
        Draft202012Validator(_plain(org_context.data_schema)).iter_errors(
            {
                "caller": "agent:owner:node:visitor",
                "capability": "org-context",
                "entryPoint": "orgfs.export",
                "resource": "accepted",
                "reason": "no grant",
            }
        )
    )
    for name in ("orgfs.export", "orgfs.import"):
        codes = {error.code for error in _spec(name).errors}
        assert {
            ipc_errors.STALE_SESSION,
            ipc_errors.SESSION_SUPERSEDED,
        } <= codes, name

    import_codes = {error.code for error in _spec("orgfs.import").errors}
    assert ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED not in import_codes


def test_mcp_session_envelope_reaches_the_write_bridge_unchanged() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class App:
        def _handle_orgfs(
            self, name: str, params: dict[str, Any], *, trusted_origin: str | None
        ) -> dict[str, Any]:
            calls.append((name, dict(params)))
            return _RESULTS["orgfs.write"]

    params = {
        **_PARAMS["orgfs.write"],
        "actor": "agent:owner:node:worker",
        "sessionRef": "mcp-session",
    }
    MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
        App(), "orgfs.write", params
    )
    assert calls == [("orgfs.write", params)]


def test_remove_takes_an_optional_boolean_permanent() -> None:
    schema = _plain(_spec("orgfs.remove").params_schema)
    assert schema["properties"]["permanent"] == {"type": "boolean"}
    assert "permanent" not in schema["required"]
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors({**_PARAMS["orgfs.remove"], "permanent": True}))
    assert not list(validator.iter_errors({**_PARAMS["orgfs.remove"], "permanent": False}))
    assert list(validator.iter_errors({**_PARAMS["orgfs.remove"], "permanent": "yes"}))


def test_restore_has_no_version() -> None:
    schema = _plain(_spec("orgfs.restore").params_schema)
    assert "version" not in schema["properties"]
    assert schema["required"] == ["spaceId", "node"]
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(_PARAMS["orgfs.restore"]))
    assert list(validator.iter_errors({**_PARAMS["orgfs.restore"], "version": "v1"}))


def test_checkout_declares_the_layout_refusal_with_its_data() -> None:
    errors = {error.code: error for error in _spec("orgfs.checkout").errors}
    refusal = errors[ipc_errors.ORGFS_LAYOUT_UNSUPPORTED]
    assert refusal.data_schema is not None
    validator = Draft202012Validator(_plain(refusal.data_schema))
    assert not list(
        validator.iter_errors(
            {"spaceId": "space-x", "reason": "legacy-journal", "guide": "docs/guide.md"}
        )
    )


def test_retired_journal_codes_are_not_declared() -> None:
    retired = {"log-key-conflict", "purged", "snapshot-barrier", "stale-plan"}
    for spec, *_handler in ORG_BINDINGS:
        if spec.name.startswith("orgfs."):
            assert retired.isdisjoint(error.code for error in spec.errors), spec.name
