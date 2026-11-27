"""Service-grant code reaches other modules only through their public names.

Decision (service grant integration fix, 2026-11-27): a name with a leading
underscore belongs to the module that defines it. The service-grant code and
the grant directory roots import such a name from no other module, so the
owning module can change it without breaking a reader it does not know of.
The service-name rule and the invalid-device quarantine are therefore public
names of service-connect and of the org network IPC module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hyprial.daemon.impl.org.network import ipc as org_ipc
from hyprial.daemon.impl.service_connect import models as connect_models
from hyprial.daemon.impl.service_grants import models as grant_models

ROOT = Path(__file__).resolve().parents[4] / "src"
SCANNED = (
    ROOT / "hyprial" / "daemon" / "impl" / "service_grants",
    ROOT / "hyprial" / "daemon" / "impl" / "org" / "network" / "grant_roots",
)


def _module_of(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")


def _private_imports() -> list[str]:
    found: list[str] = []
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            package = _module_of(base)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                    continue
                if node.module == package or node.module.startswith(package + "."):
                    continue
                for alias in node.names:
                    if alias.name.startswith("_"):
                        found.append(f"{_module_of(path)}: {node.module}.{alias.name}")
    return found


def test_no_private_name_is_imported_from_another_module() -> None:
    assert _private_imports() == []


def test_the_shared_rules_are_public_names_of_their_owners() -> None:
    assert grant_models.SERVICE_NAME_PATTERN is connect_models.SERVICE_NAME_PATTERN
    assert "invalid_device_diagnostics" in org_ipc.__all__
