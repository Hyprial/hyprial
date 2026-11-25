"""Adapter read methods keep their real daemon projections at the request boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, daemon_openapi
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import ADAPTER_START_TIMEOUT
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.daemon import daemon_catalog


ADAPTER = {
    "id": "adapter-1",
    "provider": "lark",
    "name": "lark-main",
    "status": "running",
    "online": True,
    "configured": True,
    "desired": True,
    "processRunning": True,
}


def test_adapter_reads_are_registered_for_local_operator_http() -> None:
    for name in ("adapter.list", "adapter.status"):
        binding = METHOD_REGISTRY.get(name)
        assert binding is not None
        assert binding.spec.http_exposed is True
        assert binding.spec.mutates is False
        assert binding.spec.retry == "safe"


def test_adapter_mock_cases_are_exported_from_the_same_method_specs() -> None:
    paths = daemon_openapi()["paths"]
    for name in ("adapter.list", "adapter.status"):
        assert paths[f"/daemon/request/{name}"]["post"]["x-hyprial-examples"]


def test_adapter_list_uses_existing_projection_and_rejects_extra_params() -> None:
    app = SimpleNamespace(_ipc_adapter_list=lambda: {"ok": True, "adapters": [ADAPTER]})
    assert METHOD_REGISTRY.invoke(app, "adapter.list", {}) == {
        "ok": True, "adapters": [ADAPTER]
    }
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, "adapter.list", {"actor": "forged"})
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT


def test_adapter_status_preserves_not_found_and_requires_name() -> None:
    def status(params: dict[str, object]) -> dict[str, object]:
        if params["name"] == "missing":
            raise DaemonRequestError(ipc_errors.ADAPTER_NOT_FOUND, "missing")
        return {"ok": True, "adapter": ADAPTER}

    app = SimpleNamespace(_ipc_adapter_status=status)
    assert METHOD_REGISTRY.invoke(app, "adapter.status", {"name": "lark-main"}) == {
        "ok": True, "adapter": ADAPTER
    }
    with pytest.raises(DaemonRequestError) as missing:
        METHOD_REGISTRY.invoke(app, "adapter.status", {"name": "missing"})
    assert missing.value.code == ipc_errors.ADAPTER_NOT_FOUND
    with pytest.raises(DaemonRequestError) as invalid:
        METHOD_REGISTRY.invoke(app, "adapter.status", {})
    assert invalid.value.code == ipc_errors.INVALID_ARGUMENT


def test_adapter_lifecycle_writes_use_real_handlers_and_never_retry_http() -> None:
    calls: list[tuple[str, object]] = []
    app = SimpleNamespace(
        _ipc_adapter_start=lambda params: calls.append(("start", params)) or
        {"ok": True, "changed": True, "adapter": ADAPTER},
        _ipc_adapter_stop=lambda params: calls.append(("stop", params)) or
        {"ok": True, "changed": True, "adapter": ADAPTER},
        _ipc_adapter_reload=lambda: calls.append(("reload", None)) or
        {"ok": True, "added": [], "updated": [], "removed": [], "removedRunning": []},
    )
    for name, params in (("adapter.start", {"name": "lark-main"}),
                         ("adapter.stop", {"name": "lark-main"}),
                         ("adapter.reload", {})):
        binding = METHOD_REGISTRY.get(name)
        assert binding is not None
        assert binding.spec.http_exposed is True
        assert binding.spec.mutates is True
        assert binding.spec.retry == "never"
        assert binding.spec.audience == ("local-operator",)
        assert METHOD_REGISTRY.invoke(app, name, params)["ok"] is True
        assert f"/daemon/request/{name}" in daemon_openapi()["paths"]
    assert calls == [("start", {"name": "lark-main"}), ("stop", {"name": "lark-main"}), ("reload", None)]


def test_adapter_lifecycle_routes_are_admitted_by_h03() -> None:
    entries = [dict(row, declared=True, state="available", callable=True)
               for row in daemon_catalog()["methods"]]
    view = build_view(
        {"version": "test", "nodeId": "node", "owner": "owner", "epoch": "test", "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "test", "packageVersion": "test", "methods": entries},
        socket_path="/tmp/adapter-contract.sock", local_entries=entries,
    )
    for name in ("adapter.start", "adapter.stop", "adapter.reload"):
        route = view.routes[name]
        assert route.mutates is True
        assert route.retry == "never"
        assert route.confirmation == "none"
        assert route.http_binding["identity"] == {"mode": "none", "param": None}


def test_detached_stop_keeps_existing_success_projection() -> None:
    result = {"ok": True, "changed": True,
              "adapter": {"name": "lark-main", "status": "stopped", "configured": False}}
    app = SimpleNamespace(_ipc_adapter_stop=lambda _params: result)
    assert METHOD_REGISTRY.invoke(app, "adapter.stop", {"name": "lark-main"}) == result


def test_start_preserves_lifecycle_timeout_code() -> None:
    def timeout(_params: dict[str, object]) -> None:
        raise DaemonRequestError(ADAPTER_START_TIMEOUT, "start timed out")

    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(_ipc_adapter_start=timeout),
                               "adapter.start", {"name": "lark-main"})
    assert caught.value.code == ADAPTER_START_TIMEOUT


def test_retired_pin_methods_are_absent_from_registry_and_openapi() -> None:
    for name in ("adapter.pin", "adapter.pins", "adapter.unpin"):
        assert METHOD_REGISTRY.get(name) is None
        assert f"/daemon/request/{name}" not in daemon_openapi()["paths"]
