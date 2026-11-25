"""Batch 5: hosts / top.snapshot / link.* HTTP surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _spec(name: str):
    return next(spec for spec, *_handler in ORG_BINDINGS if spec.name == name)


def test_view_methods_are_read_only_http() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    for name in ("hosts", "top.snapshot"):
        spec = _spec(name)
        assert name in registry.names
        assert (spec.mutates, spec.retry) == (False, "safe")
        assert spec.http_exposed is True
        assert spec.http_binding is not None


def test_hosts_forwards_to_the_status_view() -> None:
    calls: list[dict[str, Any]] = []

    class App:
        def _ipc_hosts(self, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(dict(params))
            return {"hosts": []}

    result = MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
        App(), "hosts", {"refresh": True}
    )
    assert result == {"hosts": []}
    assert calls == [{"refresh": True}]


def test_top_snapshot_forwards_to_the_status_view() -> None:
    calls: list[str] = []

    class App:
        def _ipc_top_snapshot(self) -> dict[str, Any]:
            calls.append("top")
            return {
                "daemon": {
                    "pid": 1,
                    "nodeId": "node-1",
                    "owner": "user:owner",
                    "epoch": "e1",
                    "dispatchWithoutPacCount": 0,
                    "dispatchConversationCount": 0,
                },
                "nowMs": 1,
                "actors": [],
                "stranded": [],
                "quota": {"sources": []},
            }

    result = MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
        App(), "top.snapshot", {}
    )
    assert result["daemon"]["nodeId"] == "node-1"
    assert calls == ["top"]


def test_hosts_result_accepts_refresh_and_real_host_rows() -> None:
    schema = _plain(_spec("hosts").result_schema)
    result = {
        "hosts": [
            {
                "nodeId": "node-1",
                "status": "online",
                "observedAtMs": 1,
                "ageMs": 0,
                "stale": False,
                "lastError": None,
            }
        ],
        "refresh": {
            "accepted": True,
            "queued": False,
            "operationId": "op-1",
            "coalesced": False,
        },
    }
    assert not list(Draft202012Validator(schema).iter_errors(result))


def test_top_snapshot_result_accepts_real_actor_rows() -> None:
    schema = _plain(_spec("top.snapshot").result_schema)
    result = {
        "daemon": {
            "pid": 1,
            "nodeId": "node-1",
            "owner": "user:owner",
            "epoch": "e1",
            "dispatchWithoutPacCount": 0,
            "dispatchConversationCount": 0,
        },
        "nowMs": 1,
        "actors": [{"actor": "agent:a", "state": "running", "pending": 0}],
        "stranded": [{"actor": "agent:b", "count": 2}],
        "quota": {"sources": []},
    }
    assert not list(Draft202012Validator(schema).iter_errors(result))
