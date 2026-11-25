"""org.list / org.fetch declarations, bindings, admission and error retention.

Decision record (org-members batch 1, 2026-11-02)
- The declarations are exercised through a registry assembled from the org
  binding tuple for schema/locality tests, and through the process-wide
  ``METHOD_REGISTRY`` for binding and profile assertions.
- The handlers are checked by replacing the two real entry points with
  recorders: the test proves the handler forwards to the real callable rather
  than re-implementing it, and that a declared error code/data pair survives
  the registry unchanged.
- ``example`` coverage is read back from the committed OpenAPI projection, so
  a declaration without a mesh/orgfs result example cannot pass silently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.org import (
    ORG_BINDINGS,
    ORG_FETCH_SPEC,
    ORG_LIST_SPEC,
    org_fetch,
    org_list,
)
from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MIGRATED_FAMILIES,
    MethodRegistry,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.request_http.catalog import build_view

PING = {
    "version": "test",
    "nodeId": "org-node",
    "owner": "org-owner",
    "epoch": "epoch-1",
    "phase": "serving",
}

_ORG_LIST_RESULT = {
    "ok": True,
    "notices": [],
    "orgs": [
        {
            "org": "group-hyprial-dev",
            "spaceId": "space-hyprial-dev",
            "role": "owner",
            "devices": 3,
            "state": "active",
            "authorityDevice": True,
            "kind": "work",
            "current": False,
        },
        {
            "org": "group-acme-lab",
            "spaceId": None,
            "role": None,
            "devices": 0,
            "state": "legacy-active",
            "authorityDevice": None,
            "kind": "work",
            "current": False,
        },
    ],
    "leftOrgs": ["group-old"],
}

_ORG_FETCH_MESH = {
    "requestedSources": ["node-2"],
    "responseCount": 1,
    "receivedCount": 1,
    "rejectedCount": 0,
    "candidates": [
        {
            "path": "/tmp/example/pending.json",
            "version": 7,
            "publisher": "user:lin",
            "receivedAt": "2026-11-02T09:30:00Z",
            "source": "node-2",
        }
    ],
}

_ORG_FETCH_ORGFS = {
    **_ORG_FETCH_MESH,
    "source": "orgfs",
    "refreshed": True,
    "spaceId": "space-hyprial-context",
    "deprecatedSourceTarget": True,
    "sourceTargetHint": "use --from user:<name>",
}


def _accepts(schema: Any, value: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(value))


class _FetchApp:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = _ORG_FETCH_MESH if result is None else result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def _ipc_org_fetch(self, params: dict[str, Any]) -> Any:
        self.calls.append(dict(params))
        if self.error is not None:
            raise self.error
        return self.result


def _view() -> Any:
    entries = [
        dict(row, declared=True, state="available", callable=True)
        for row in daemon_catalog()["methods"]
    ]
    return build_view(
        PING,
        {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": "test",
            "methods": entries,
        },
        socket_path="/tmp/org-declaration-test.sock",
        local_entries=entries,
    )


def test_the_two_org_read_methods_are_declared_and_bound_one_to_one() -> None:
    assert [spec.name for spec in (ORG_FETCH_SPEC, ORG_LIST_SPEC)] == [
        "org.fetch",
        "org.list",
    ]
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert registry.names == (
        "hosts",
        "org.acl.grant",
        "org.acl.ls",
        "org.acl.revoke",
        "org.create",
        "org.delete",
        "org.execute",
        "org.fetch",
        "org.invite",
        "org.invites.accept",
        "org.invites.list",
        "org.join",
        "org.leave",
        "org.list",
        "org.network",
        "org.publish",
        "org.remove",
        "org.repair",
        "org.resolve",
        "org.use",
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
        "top.inspect",
        "top.snapshot",
    )
    assert METHOD_REGISTRY.get("org.list").handler is org_list
    assert METHOD_REGISTRY.get("org.fetch").handler is org_fetch
    assert "org" not in MIGRATED_FAMILIES


def test_org_profiles_match_the_read_and_write_pictures() -> None:
    listed = METHOD_REGISTRY.get("org.list").spec
    fetched = METHOD_REGISTRY.get("org.fetch").spec
    assert (listed.mutates, listed.retry, listed.confirmation) == (
        False,
        "safe",
        "none",
    )
    assert (fetched.mutates, fetched.retry, fetched.confirmation) == (
        True,
        "never",
        "none",
    )
    for spec in (listed, fetched):
        assert spec.http_exposed is True
        assert spec.mode == "unary"
        assert spec.http_binding is not None
        assert spec.http_binding.identity.mode == "none"
        assert spec.http_binding.identity.param is None
        assert spec.idempotency is not None
        assert spec.idempotency.mode == spec.retry


@pytest.mark.parametrize("params", [[], None, "list", {"org": "group-x"}, {"extra": 1}])
def test_org_list_params_are_the_empty_object(params: Any) -> None:
    assert not _accepts(ORG_LIST_SPEC.params_schema, params)


def test_org_list_params_accept_the_empty_object() -> None:
    assert _accepts(ORG_LIST_SPEC.params_schema, {})


@pytest.mark.parametrize(
    "timeout",
    [0, -1, 31, True, False, "2", None],
)
def test_org_fetch_rejects_timeouts_outside_the_declared_bound(timeout: Any) -> None:
    assert not _accepts(
        ORG_FETCH_SPEC.params_schema, {"timeoutSeconds": timeout}
    )


@pytest.mark.parametrize("timeout", [0.5, 2, 30])
def test_org_fetch_accepts_positive_timeouts_within_the_bound(timeout: Any) -> None:
    assert _accepts(ORG_FETCH_SPEC.params_schema, {"timeoutSeconds": timeout})


@pytest.mark.parametrize(
    "params",
    [
        {"from": ""},
        {"from": 5},
        {"requestMode": "batch"},
        {"requestMode": "neighbors", "unexpected": True},
    ],
)
def test_org_fetch_rejects_invalid_selectors(params: dict[str, Any]) -> None:
    assert not _accepts(ORG_FETCH_SPEC.params_schema, params)


def test_org_fetch_accepts_the_neighbors_mode_and_wire_selectors() -> None:
    assert _accepts(
        ORG_FETCH_SPEC.params_schema,
        {"timeoutSeconds": 2, "requestMode": "neighbors", "from": "node-2"},
    )


def test_org_list_result_accepts_null_role_and_null_space_id() -> None:
    assert _accepts(ORG_LIST_SPEC.result_schema, _ORG_LIST_RESULT)


def test_org_fetch_result_accepts_mesh_and_orgfs_shapes() -> None:
    assert _accepts(ORG_FETCH_SPEC.result_schema, _ORG_FETCH_MESH)
    assert _accepts(ORG_FETCH_SPEC.result_schema, _ORG_FETCH_ORGFS)


@pytest.mark.parametrize(
    "result",
    [
        {**_ORG_FETCH_MESH, "source": "mesh"},
        {**_ORG_FETCH_MESH, "requestedSources": None},
        {**_ORG_FETCH_MESH, "candidates": [{"path": "/tmp/x"}]},
        {"requestedSources": [], "responseCount": 0},
    ],
)
def test_org_fetch_result_rejects_undeclared_shapes(result: dict[str, Any]) -> None:
    assert not _accepts(ORG_FETCH_SPEC.result_schema, result)


def test_org_list_handler_forwards_to_the_real_ipc_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    calls: list[tuple[Any, dict[str, Any]]] = []

    def recorder(app: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((app, dict(params)))
        return _ORG_LIST_RESULT

    monkeypatch.setattr(org_ipc, "ipc_org_list", recorder)
    app = SimpleNamespace(hyprial_home=tmp_path, _log=lambda *args, **kw: None)
    result = METHOD_REGISTRY.invoke(app, "org.list", {})
    assert result == _ORG_LIST_RESULT
    assert calls == [(app, {})]


def test_org_fetch_handler_forwards_to_the_real_app_entry() -> None:
    app = _FetchApp()
    result = METHOD_REGISTRY.invoke(
        app, "org.fetch", {"timeoutSeconds": 2, "requestMode": "neighbors"}
    )
    assert result == _ORG_FETCH_MESH
    assert app.calls == [{"timeoutSeconds": 2, "requestMode": "neighbors"}]


def test_org_fetch_reports_not_ready_when_the_app_has_no_entry() -> None:
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "org.fetch", {})
    assert raised.value.code == ipc_errors.DAEMON_NOT_READY


def test_declared_org_fetch_errors_keep_their_code_and_data() -> None:
    unreachable = DaemonRequestError(
        ipc_errors.ORG_SOURCE_UNREACHABLE,
        "source is not online",
        {"availableSources": ["node-2"]},
    )
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(_FetchApp(error=unreachable), "org.fetch", {})
    assert raised.value.code == ipc_errors.ORG_SOURCE_UNREACHABLE
    assert raised.value.data == {"availableSources": ["node-2"]}

    not_joined = DaemonRequestError(
        ipc_errors.ORG_SPACE_NOT_JOINED,
        "not joined",
        {"hint": "hyprial org fs space join <spaceId>"},
    )
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(_FetchApp(error=not_joined), "org.fetch", {})
    assert raised.value.code == ipc_errors.ORG_SPACE_NOT_JOINED
    assert raised.value.data == {"hint": "hyprial org fs space join <spaceId>"}


def test_org_list_network_fallback_survives_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    def failing(app: Any, params: dict[str, Any]) -> dict[str, Any]:
        raise DaemonRequestError("ORG_NETWORK_ERROR", "orgfs failed")

    monkeypatch.setattr(org_ipc, "ipc_org_list", failing)
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "org.list", {})
    assert raised.value.code == "ORG_NETWORK_ERROR"


def test_declared_lowercase_orgfs_codes_keep_their_code_and_data() -> None:
    app = _FetchApp(
        error=DaemonRequestError(
            "ambiguous-space",
            "duplicate spaces",
            {"space": "group-hyprial-dev", "count": 2},
        )
    )
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, "org.fetch", {})
    assert raised.value.code == "ambiguous-space"
    assert raised.value.data == {"space": "group-hyprial-dev", "count": 2}


def test_an_unfrozen_lowercase_code_cannot_claim_the_contract() -> None:
    app = _FetchApp(error=DaemonRequestError("brand-new-code", "not declared"))
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, "org.fetch", {})
    assert raised.value.code == ipc_errors.DAEMON_ERROR


def test_org_reads_declare_one_code_per_condition() -> None:
    for spec in (ORG_LIST_SPEC, ORG_FETCH_SPEC):
        codes = {error.code for error in spec.errors}
        assert {"ambiguous-space", "unknown-space", "unavailable"} <= codes
        assert ipc_errors.INVALID_ARGUMENT in codes
        assert "invalid-argument" not in codes
    fetch_codes = {error.code for error in ORG_FETCH_SPEC.errors}
    assert {
        "content-pending",
        ipc_errors.ORG_PATH_SHAPE_INVALID,
        "ambiguous-path",
    } <= fetch_codes
    for code in (ipc_errors.ORG_PATH_SHAPE_INVALID, "ambiguous-path"):
        error = next(item for item in ORG_FETCH_SPEC.errors if item.code == code)
        assert error.data_schema is None
    descriptions = {error.description for error in ORG_FETCH_SPEC.errors}
    assert len(descriptions) == len(ORG_FETCH_SPEC.errors)


def test_org_fetch_translates_the_orgfs_path_shape_code() -> None:
    app = _FetchApp(error=DaemonRequestError("invalid-argument", "not a directory"))
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, "org.fetch", {})
    assert raised.value.code == ipc_errors.ORG_PATH_SHAPE_INVALID


def test_org_list_schema_rejection_stays_inside_its_declaration() -> None:
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "org.list", {"unexpected": True})
    assert raised.value.code == ipc_errors.INVALID_ARGUMENT
    declared = {error.code for error in ORG_LIST_SPEC.errors}
    assert raised.value.code in declared


def test_ambiguous_space_data_accepts_every_real_producer_shape() -> None:
    for spec in (ORG_LIST_SPEC, ORG_FETCH_SPEC):
        error = next(item for item in spec.errors if item.code == "ambiguous-space")
        assert error.data_schema is not None
        assert _accepts(error.data_schema, {"space": "group-x", "count": 2})
        assert _accepts(
            error.data_schema,
            {"org": "group-x", "space": "group-x", "count": 2},
        )
        assert _accepts(error.data_schema, {"org": "group-x"})
        assert not _accepts(error.data_schema, {"space": "group-x"})


def test_the_legacy_router_does_not_shadow_the_declared_org_methods() -> None:
    router = (
        Path(__file__).resolve().parents[5]
        / "src"
        / "hyprial"
        / "daemon"
        / "impl"
        / "ipc"
        / "router.py"
    ).read_text(encoding="utf-8")
    assert '"org.list": "ipc_org_list"' not in router
    assert 'if method == "org.fetch"' not in router
    for name in ("org.create", "org.invite", "org.join"):
        assert f'"{name}": "ipc_org_' not in router


def test_both_org_methods_are_admitted_with_their_profiles() -> None:
    view = _view()
    assert "org.list" in view.routes
    assert "org.fetch" in view.routes
    listed = view.routes["org.list"]
    assert (listed.mutates, listed.retry, listed.confirmation) == (
        False,
        "safe",
        "none",
    )
    fetched = view.routes["org.fetch"]
    assert (fetched.mutates, fetched.retry, fetched.confirmation) == (
        True,
        "never",
        "none",
    )
    assert fetched.http_binding["identity"] == {"mode": "none", "param": None}


def test_the_projection_carries_mesh_orgfs_and_error_examples() -> None:
    methods = {entry["name"]: entry for entry in daemon_openapi()["x-hyprial-methods"]}
    assert methods["org.list"]["httpExposed"] is True
    assert methods["org.fetch"]["httpExposed"] is True
    fetch_examples = methods["org.fetch"]["x-hyprial-examples"]
    assert any(case["result"].get("source") == "orgfs" for case in fetch_examples)
    assert any(
        case["error"]["code"] == ipc_errors.ORG_SOURCE_UNREACHABLE
        and case["error"]["data"] == {"availableSources": ["node-2"]}
        for case in fetch_examples
        if case.get("error")
    )
    assert any(case["kind"] == "invalid-params" for case in fetch_examples)
