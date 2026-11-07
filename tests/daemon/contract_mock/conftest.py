"""Fixtures for the M-03 example, mock and drift checks.

The synthetic catalog is deliberately unrelated to the five Agent methods: it
proves the generic example contract before any accepted M-01/M-02 declaration
exists.  Nothing here reads a home, socket, credential store or production
configuration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_MOCK_ROOT = ROOT / "contract" / "daemon-request"
if str(CONTRACT_MOCK_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTRACT_MOCK_ROOT))

from hyprial.kernel import (  # noqa: E402
    MethodError,
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    MethodSpec,
    export_openapi,
)


def _demo_list_spec() -> MethodSpec:
    return MethodSpec(
        name="demo.list",
        owner="daemon.demo",
        params_schema={
            "type": "object",
            "properties": {"filter": {"type": ["string", "null"]}},
            "required": ["filter"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {
                "ok": {"const": True},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ok", "items"],
            "additionalProperties": False,
        },
        errors=(
            MethodError("INVALID_ARGUMENT"),
            MethodError(
                "DAEMON_NOT_READY",
                data_schema={
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            ),
        ),
        http_exposed=True,
    )


def _demo_write_spec() -> MethodSpec:
    return MethodSpec(
        name="demo.write",
        owner="daemon.demo",
        params_schema={
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "setupToken": {"type": "string", "minLength": 1},
            },
            "required": ["actor", "setupToken"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {
                "ok": {"const": True},
                "actor": {"type": "string", "minLength": 1},
                "removed": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ok", "actor", "removed"],
            "additionalProperties": False,
        },
        errors=(
            MethodError("INVALID_ARGUMENT"),
            MethodError(
                "TARGET_NOT_AUTHORIZED",
                data_schema={
                    "type": "object",
                    "properties": {"actor": {"type": "string"}},
                    "required": ["actor"],
                    "additionalProperties": False,
                },
            ),
        ),
        mutates=True,
        retry="never",
        http_exposed=True,
    )


def demo_specs() -> tuple[MethodSpec, ...]:
    return (_demo_list_spec(), _demo_write_spec())


def demo_example_sets() -> tuple[MethodExampleSet, ...]:
    sentinel = "example-not-a-credential"
    return (
        MethodExampleSet(
            method="demo.list",
            cases=(
                MethodExample(
                    name="all",
                    kind="success",
                    description="one matching item is present",
                    params={"filter": None},
                    result={"ok": True, "items": ["alpha"]},
                ),
                MethodExample(
                    name="no-match",
                    kind="empty",
                    description="a valid filter that matches nothing",
                    params={"filter": "missing"},
                    result={"ok": True, "items": []},
                    reason="an empty match is a legal successful result",
                ),
                MethodExample(
                    name="wrong-filter-type",
                    kind="invalid-params",
                    description="the filter type is rejected before dispatch",
                    params={"filter": 5},
                    error=MethodExampleError(
                        "INVALID_ARGUMENT", "demo.list params do not match the schema"
                    ),
                ),
                MethodExample(
                    name="non-object-body",
                    kind="invalid-params",
                    description="a non-object body is structurally invalid",
                    params=[],
                    error=MethodExampleError(
                        "INVALID_ARGUMENT", "params must be a JSON object"
                    ),
                ),
                MethodExample(
                    name="daemon-restoring",
                    kind="domain-error",
                    description="the daemon is restoring and cannot answer",
                    params={"filter": None},
                    error=MethodExampleError(
                        "DAEMON_NOT_READY",
                        "the daemon is restoring",
                        {"reason": "restore"},
                    ),
                ),
                MethodExample(
                    name="unknown-filter",
                    kind="semantic-param-error",
                    description="a schema-valid filter is rejected by the handler",
                    params={"filter": "unknown"},
                    error=MethodExampleError(
                        "INVALID_ARGUMENT", "unknown filter"
                    ),
                ),
            ),
        ),
        MethodExampleSet(
            method="demo.write",
            empty_not_applicable=(
                "the write either returns its removal receipt or fails; it has no "
                "legal empty result"
            ),
            cases=(
                MethodExample(
                    name="removed",
                    kind="success",
                    description="one synthetic file was removed",
                    params={"actor": "agent-a", "setupToken": sentinel},
                    result={"ok": True, "actor": "agent-a", "removed": ["provider.json"]},
                ),
                MethodExample(
                    name="missing-actor",
                    kind="invalid-params",
                    description="an empty actor is rejected before dispatch",
                    params={"actor": "", "setupToken": sentinel},
                    error=MethodExampleError(
                        "INVALID_ARGUMENT", "actor must be a non-empty string"
                    ),
                ),
                MethodExample(
                    name="foreign-target",
                    kind="domain-error",
                    description="the caller may not mutate another agent",
                    params={"actor": "agent-b", "setupToken": sentinel},
                    error=MethodExampleError(
                        "TARGET_NOT_AUTHORIZED",
                        "the target agent is not authorized",
                        {"actor": "agent-b"},
                    ),
                ),
            ),
        ),
    )


def demo_openapi() -> dict:
    return export_openapi(demo_specs(), example_sets=demo_example_sets())


@pytest.fixture()
def example_sets() -> tuple[MethodExampleSet, ...]:
    return demo_example_sets()


@pytest.fixture()
def openapi_document() -> dict:
    return demo_openapi()


@pytest.fixture()
def contract_file(tmp_path: Path, openapi_document: dict) -> Path:
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(openapi_document, indent=2), encoding="utf-8")
    return path
