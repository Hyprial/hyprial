"""Batch 2: ``org.network`` / ``org.repair`` / ``org.publish`` declarations.

The three names were served by the legacy IPC router without a MethodSpec.
These tests pin their HTTP profiles, their forwarding handlers and the error
codes their real implementations can raise.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MethodRegistry,
)
from hyprial.kernel import ipc_errors

_EXPECTED = (
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
    "orgfs.import",
    "orgfs.invite",
    "orgfs.join",
    "orgfs.ls",
    "orgfs.members",
    "orgfs.mkdir",
    "orgfs.move",
    "orgfs.read",
    "orgfs.remove",
    "orgfs.remove_member",
    "orgfs.resolve",
    "orgfs.restore",
    "orgfs.space_purge",
    "orgfs.spaces",
    "orgfs.stat",
    "orgfs.status",
    "orgfs.trash",
    "orgfs.watch",
    "orgfs.write",
    "top.inspect",
    "top.snapshot",
)

_NETWORK_RESULT = {
    "orgs": [],
    "unavailableOrgs": [],
    "dataProblems": [],
    "directory": {},
    "directoryPending": {},
    "self": {
        "deviceId": "node",
        "owner": "owner",
        "serverPublic": "nodekey:server",
        "addressPresent": False,
    },
    "mappedPeers": None,
    "sidecar": None,
    "paneStatus": {
        "directory": {"available": True},
        "mappedPeers": {"available": True},
        "sidecar": {"available": True},
    },
    "sources": {
        "directory": "orgfs",
        "mappedPeers": "forwarding-sidecar",
        "sidecar": "forwarding-sidecar",
    },
}

_RESULTS = {
    "org.network": _NETWORK_RESULT,
    "org.repair": {"apply": False, "candidates": [], "repaired": []},
}

_PARAMS = {
    "org.network": {},
    "org.repair": {},
}

_ENTRYPOINTS = {
    "org.network": "ipc_org_network",
    "org.repair": "ipc_org_repair",
}


def test_batch_two_registry_and_profiles() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert registry.names == _EXPECTED
    specs = {spec.name: spec for spec, *_handler in ORG_BINDINGS}
    network = specs["org.network"]
    assert (network.mutates, network.retry, network.confirmation) == (
        False,
        "safe",
        "none",
    )
    for name in ("org.repair", "org.publish"):
        spec = specs[name]
        assert (spec.mutates, spec.retry, spec.confirmation) == (
            True,
            "never",
            "none",
        )
    for name in ("org.network", "org.repair", "org.publish"):
        spec = specs[name]
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.audience == ("local-operator",)


@pytest.mark.parametrize("method", list(_RESULTS))
def test_each_network_handler_forwards_to_its_ipc_entry(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    calls: list[tuple[str, Any, dict[str, Any]]] = []

    def recorder(app: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, app, dict(params)))
        return _RESULTS[method]

    monkeypatch.setattr(org_ipc, _ENTRYPOINTS[method], recorder)
    app = object()
    result = METHOD_REGISTRY.invoke(app, method, _PARAMS[method])
    assert result == _RESULTS[method]
    assert calls == [(method, app, _PARAMS[method])]


def test_publish_forwards_to_the_real_application_entry() -> None:
    calls: list[str] = []

    class App:
        def _ipc_org_publish(self) -> dict[str, Any]:
            calls.append("publish")
            return {"published": True, "orgfsPublished": True}

    result = METHOD_REGISTRY.invoke(App(), "org.publish", {})
    assert result == {"published": True, "orgfsPublished": True}
    assert calls == ["publish"]


def test_publish_reports_not_ready_when_the_app_has_no_entry() -> None:
    from hyprial.kernel import DaemonRequestError

    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "org.publish", {})
    assert raised.value.code == ipc_errors.DAEMON_NOT_READY


def test_batch_two_declarations_keep_the_real_error_codes() -> None:
    specs = {spec.name: spec for spec, *_handler in ORG_BINDINGS}
    network = {error.code for error in specs["org.network"].errors}
    assert {
        "MEMBERSHIP_NOT_INITIALIZED",
        "MEMBERSHIP_SIDECAR_TIMEOUT",
        "DEVICE_RECORD_MISSING",
        "FORWARDING_UNAVAILABLE",
        "ORG_NETWORK_ERROR",
    } <= network
    repair = {error.code for error in specs["org.repair"].errors}
    assert {
        "CASDOOR_GROUP_UNAVAILABLE",
        "CASDOOR_ORG_QUOTA_EXCEEDED",
        "MEMBERSHIP_STORE_INVALID",
        ipc_errors.DAEMON_NOT_READY,
    } <= repair
    publish = {error.code for error in specs["org.publish"].errors}
    assert {ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_NOT_READY} <= publish


def test_the_legacy_router_no_longer_shadows_batch_two() -> None:
    router = (
        Path(__file__).resolve().parents[5]
        / "src"
        / "hyprial"
        / "daemon"
        / "impl"
        / "ipc"
        / "router.py"
    ).read_text(encoding="utf-8")
    assert '"org.network": "ipc_org_network"' not in router
    assert '"org.repair": "ipc_org_repair"' not in router
    assert 'if method == "org.publish"' not in router


def _network_spec():
    return next(spec for spec, *_handler in ORG_BINDINGS if spec.name == "org.network")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _accepts(value: Any) -> bool:
    validator = Draft202012Validator(_plain(_network_spec().result_schema))
    return not list(validator.iter_errors(value))


def test_sidecar_last_poll_accepts_the_real_boolean_peers_reported() -> None:
    result = {
        **_NETWORK_RESULT,
        "sidecar": {
            "policy": {"mode": "on", "source": "config"},
            "endpoints": ["tcp/127.0.0.1:1234"],
            "dialed": ["tcp/127.0.0.1:1234"],
            "restartRequired": False,
            "lastPoll": {
                "atMs": 1_790_000_000_000,
                "ok": True,
                "error": None,
                "peersReported": True,
                "peerCount": 1,
                "derpMapURL": None,
                "derpMapSource": None,
            },
            "slots": None,
            "connectionTelemetry": {"state": "unavailable"},
            "derpMapURL": None,
            "derpMapSource": None,
        },
    }
    assert _accepts(result)


def test_mapped_peers_accepts_the_real_forwarding_array() -> None:
    result = {
        **_NETWORK_RESULT,
        "mappedPeers": [
            {
                "peer": "node-2",
                "endpoint": "100.64.51.2:41641",
                "dialed": True,
                "slot": 0,
            },
            {"peer": "node-3", "endpoint": None, "dialed": False},
        ],
    }
    assert _accepts(result)


def test_mapped_peers_rejects_the_old_object_shape() -> None:
    result = {**_NETWORK_RESULT, "mappedPeers": {"node-2": "100.64.51.2:41641"}}
    assert not _accepts(result)


def test_data_problems_keep_the_real_string_version() -> None:
    result = {
        **_NETWORK_RESULT,
        "dataProblems": [
            {
                "org": "group-x",
                "path": "directory/user/device.json",
                "reason": "invalid-device-record",
                "version": "eyJjb250ZW50RnJvbnRpZXIiOjJ9",
            }
        ],
    }
    assert _accepts(result)


def test_directory_pending_accepts_the_real_pending_object() -> None:
    result = {
        **_NETWORK_RESULT,
        "directoryPending": {
            "group-x": {
                "contentPending": [
                    {
                        "owner": "user:owner",
                        "deviceId": "device-a",
                        "reason": "content-pending",
                    }
                ],
                "dataProblems": [
                    {
                        "org": "group-x",
                        "path": "directory/user/device.json",
                        "reason": "content-pending",
                        "version": "frontier-2",
                    }
                ],
            }
        },
    }
    assert _accepts(result)


def test_directory_pending_rejects_string_items() -> None:
    result = {
        **_NETWORK_RESULT,
        "directoryPending": {
            "group-x": {
                "contentPending": ["device-a"],
            }
        },
    }
    assert not _accepts(result)


def test_directory_pending_rejects_the_removed_parked_commits_field() -> None:
    result = {
        **_NETWORK_RESULT,
        "directoryPending": {
            "group-x": {"contentPending": [], "parkedCommits": []}
        },
    }
    assert not _accepts(result)
