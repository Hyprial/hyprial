"""Slice 7: no ``transfer.*`` daemon method survives anywhere; ``teleport.run`` stays.

Decisions are recorded in ``support.py``.  The registry, the live catalog, the
live OpenAPI document and the committed snapshot are read through their
public functions.  The legacy router is checked through its stats table and
through an AST scan for the exact legacy method names, because those names
were only ever served by string-compare dispatch branches.
"""

from __future__ import annotations

import ast
import json

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.request_catalog import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

from .support import LEGACY_METHODS, ROOT, product_modules

SNAPSHOT = ROOT / "contract" / "daemon-request" / "openapi.json"


def _transfer(names) -> list[str]:
    return sorted(name for name in names if name.startswith("transfer."))


def _openapi_names(document: dict) -> tuple[list[str], list[str]]:
    methods = [entry["name"] for entry in document["x-hyprial-methods"]]
    paths = [path.rsplit("/", 1)[-1] for path in document["paths"]]
    return methods, paths


def test_registry_declares_teleport_and_no_transfer_method() -> None:
    assert "teleport.run" in METHOD_REGISTRY
    assert _transfer(METHOD_REGISTRY.names) == []
    assert _transfer(MIGRATED_METHODS) == []


def test_live_catalog_lists_teleport_and_no_transfer_method() -> None:
    names = [entry["name"] for entry in daemon_catalog()["methods"]]
    assert "teleport.run" in names
    assert _transfer(names) == []


def test_live_and_committed_openapi_carry_no_transfer_method() -> None:
    for document in (daemon_openapi(), json.loads(SNAPSHOT.read_text(encoding="utf-8"))):
        methods, paths = _openapi_names(document)
        assert "teleport.run" in methods
        assert _transfer(methods) == []
        assert _transfer(paths) == []


def test_legacy_router_counts_no_transfer_method() -> None:
    assert _transfer(_IPC_STATS_METHODS) == []


def test_application_exposes_no_transfer_ipc_entry_point() -> None:
    entries = sorted(name for name in dir(DaemonApplication) if name.startswith("_ipc_transfer"))
    assert entries == []


def test_no_product_code_names_a_legacy_transfer_method() -> None:
    hits: list[str] = []
    for name, module in sorted(product_modules().items()):
        for node in ast.walk(module.parse()):
            if isinstance(node, ast.Constant) and node.value in LEGACY_METHODS:
                hits.append(f"  {name}:{node.lineno} {node.value}")
    assert hits == [], "legacy transfer.* method names still in src:\n" + "\n".join(hits)
