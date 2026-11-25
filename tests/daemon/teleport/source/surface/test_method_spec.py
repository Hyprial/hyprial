"""``teleport.run``: the declared MethodSpec, its schemas and its OpenAPI row.

Decisions are recorded in ``tests/daemon/teleport/source/fakes.py``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import ipc_errors, validate_method_spec

from ...support import FROZEN_ERROR_CODES

CATALOG = "hyprial.daemon.impl.request_catalog.teleport"
ROOT = Path(__file__).resolve().parents[5]
SNAPSHOT = ROOT / "contract" / "daemon-request" / "openapi.json"

SUCCESS = {
    "ok": True,
    "operationId": "op-1",
    "dryRun": False,
    "agent": "w1",
    "from": {"node": "node-a", "uri": "agent://owner-1/node-a/w1"},
    "to": {"node": "node-b", "uri": "agent://owner-1/node-b/w1"},
    "bytes": 4096,
    "routines": [{"name": "nightly", "enabled": True}],
    "commit": {"ok": False, "pausedRoutines": ["nightly"]},
}
DRY_RUN = {
    "ok": True,
    "operationId": "op-2",
    "dryRun": True,
    "agent": "w1",
    "from": {"node": "node-a", "uri": "agent://owner-1/node-a/w1"},
    "to": {"node": "node-b", "uri": None},
    "bytes": 0,
    "routines": [],
}


def _spec():
    return importlib.import_module(CATALOG).TELEPORT_RUN_SPEC


def _plain(value):
    return json.loads(json.dumps(value, default=dict))


def test_spec_is_registered_and_bound() -> None:
    spec = _spec()
    validate_method_spec(spec)
    binding = METHOD_REGISTRY.get("teleport.run")
    assert binding is not None
    assert binding.spec is spec
    assert "teleport.run" in MIGRATED_METHODS
    names = {name for name in METHOD_REGISTRY.names if "teleport" in name}
    assert names == {"teleport.run"}


def test_spec_declares_a_non_retryable_operator_write() -> None:
    spec = _spec()
    assert spec.name == "teleport.run"
    assert spec.owner == "daemon.teleport"
    assert spec.mode == "unary"
    assert spec.mutates is True
    assert spec.retry == "never"
    assert spec.confirmation == "none"
    assert spec.preview is None and spec.preview_of is None
    assert spec.audience == ("local-operator",)
    assert spec.http_exposed is False
    assert spec.ipc_param_validation == "schema"
    if spec.idempotency is not None:
        assert spec.idempotency.mode == "never"


def test_params_are_agent_to_and_optional_dry_run_only() -> None:
    schema = _plain(_spec().params_schema)
    assert set(schema["properties"]) == {"agent", "to", "dryRun"}
    assert sorted(schema["required"]) == ["agent", "to"]
    assert schema["additionalProperties"] is False
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"agent": "w1", "to": "node-b"})
    assert validator.is_valid({"agent": "w1", "to": "node-b", "dryRun": True})
    for bad in (
        {"agent": "w1"},
        {"to": "node-b"},
        {"agent": "", "to": "node-b"},
        {"agent": "w1", "to": ""},
        {"agent": "w1", "to": "node-b", "dryRun": "yes"},
        {"agent": "w1", "to": "node-b", "yes": True},
    ):
        assert not validator.is_valid(bad), bad


def test_result_schema_accepts_run_and_dry_run_and_is_closed() -> None:
    validator = Draft202012Validator(_plain(_spec().result_schema))
    assert validator.is_valid(SUCCESS)
    assert validator.is_valid({**SUCCESS, "commit": {"ok": True, "pausedRoutines": []}})
    assert validator.is_valid(DRY_RUN)
    assert not validator.is_valid({**SUCCESS, "extra": 1})
    assert not validator.is_valid({**SUCCESS, "ok": False})
    for key in ("operationId", "dryRun", "agent", "from", "to", "bytes", "routines"):
        partial = dict(SUCCESS)
        del partial[key]
        assert not validator.is_valid(partial), key


def test_declared_errors_are_every_teleport_code_and_three_existing_ones() -> None:
    codes = {error.code for error in _spec().errors}
    assert codes == FROZEN_ERROR_CODES | {
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.AGENT_NOT_FOUND,
        ipc_errors.USER_AGENT_IMMUTABLE,
    }


@pytest.mark.parametrize(
    ("code", "sample"),
    [
        (
            "TELEPORT_UNRESOLVED",
            {
                "operationId": "op-1",
                "targetNode": "node-b",
                "agent": "w1",
                "sourceAgentUri": "agent://owner-1/node-a/w1",
                "bytesSent": 4096,
                "sourceState": "stopped",
                "pausedRoutines": [],
                "manualCheck": "look for w1 on node-b",
            },
        ),
        (
            "TELEPORT_SOURCE_CLEANUP_FAILED",
            {
                "operationId": "op-1",
                "agent": "w1",
                "targetNode": "node-b",
                "targetAgentUri": "agent://owner-1/node-b/w1",
                "pausedRoutines": ["nightly"],
            },
        ),
    ],
)
def test_manual_follow_up_errors_declare_closed_data(code: str, sample: dict) -> None:
    error = next(item for item in _spec().errors if item.code == code)
    assert error.data_schema is not None
    schema = _plain(error.data_schema)
    assert sorted(schema["required"]) == sorted(sample)
    assert schema["additionalProperties"] is False
    validator = Draft202012Validator(schema)
    assert validator.is_valid(sample)
    assert not validator.is_valid({**sample, "extra": True})
    if code == "TELEPORT_UNRESOLVED":
        assert not validator.is_valid({**sample, "sourceState": "running"})


def test_openapi_export_and_committed_snapshot_carry_the_method() -> None:
    from hyprial.daemon import daemon_openapi

    exported = {m["name"]: m for m in daemon_openapi()["x-hyprial-methods"]}
    assert "teleport.run" in exported
    assert exported["teleport.run"]["mutates"] is True
    assert "/daemon/request/teleport.run" not in daemon_openapi()["paths"]
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    committed = {m["name"]: m for m in snapshot["x-hyprial-methods"]}
    assert committed.get("teleport.run") == exported["teleport.run"]
