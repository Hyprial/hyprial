"""Freeze the historical lowercase-kebab error-code exception list.

Decision record (org-members batch 1 fix 2, 2026-11-02)
- ``LEGACY_KEBAB_CODES`` is a compatibility snapshot, not a pattern.  The
  snapshot test must be edited deliberately when a code changes; growing the
  list needs a reviewed contract change and a PR reason.
- The producer scan covers the factories whose codes reach a daemon request:
  ``OrgFsError``, ``StoreError``, ``DaemonRequestError`` and the org/forwarding
  wrappers that translate into it.  It deliberately does not inspect every
  string literal, so ordinary reasons and messages are not false positives.
- Both HTTP contract JSON files must carry the same enum as the kernel set.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

from hyprial.daemon.impl.orgfs.api import ORGFS_ERROR_CODES
from hyprial.kernel import (
    LEGACY_KEBAB_CODES,
    ipc_errors,
    is_declared_error_code,
)

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

FROZEN_LEGACY_CODES = frozenset(
    {
        "already-exists",
        "ambiguous-path",
        "ambiguous-space",
        "bad-cursor",
        "blob-unavailable",
        "checkout-io",
        "closed",
        "content-pending",
        "cross-space-uri",
        "invalid-argument",
        "invalid-uri",
        "log-key-conflict",
        "no-holder-online",
        "not-a-member",
        "not-owner",
        "out-of-range",
        "pending-meta",
        "projection-behind",
        "protected-path",
        "purged",
        "resource-exhausted",
        "snapshot-barrier",
        "snapshot-unavailable",
        "space-purge-incomplete",
        "space-purge-refused",
        "stale-plan",
        "stale-write",
        "sync-peer-incompatible",
        "too-large",
        "unavailable",
        "unknown-blob",
        "unknown-doc",
        "unknown-space",
        "unsafe-space-path",
    }
)

_REQUEST_ERROR_FACTORIES = frozenset(
    {
        "DaemonRequestError",
        "ForwardingSidecarError",
        "InviteError",
        "OrgFsError",
        "OrgMigrationError",
        "OrgNetworkError",
        "StoreError",
    }
)
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")


def _factory_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _code_literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ipc_errors"
    ):
        value = getattr(ipc_errors, node.attr, None)
        return value if isinstance(value, str) else None
    return None


def _producer_codes() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _factory_name(node) not in _REQUEST_ERROR_FACTORIES:
                continue
            candidates: list[str | None] = []
            if node.args:
                candidates.append(_code_literal(node.args[0]))
            for keyword in node.keywords:
                if keyword.arg in ("code", "error_code"):
                    candidates.append(_code_literal(keyword.value))
            for code in candidates:
                if code is not None and _KEBAB.fullmatch(code):
                    found.setdefault(code, []).append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_the_legacy_list_is_frozen() -> None:
    assert LEGACY_KEBAB_CODES == FROZEN_LEGACY_CODES
    assert ORGFS_ERROR_CODES <= LEGACY_KEBAB_CODES


def test_the_predicate_accepts_upper_and_only_frozen_lowercase() -> None:
    assert is_declared_error_code("INVALID_ARGUMENT")
    assert is_declared_error_code("NEW_UPPER_CODE_2")
    assert is_declared_error_code("ambiguous-space")
    assert not is_declared_error_code("brand-new-code")
    assert not is_declared_error_code("Mixed-Case")
    assert not is_declared_error_code(None)  # type: ignore[arg-type]


def test_producer_lowercase_error_literals_are_frozen() -> None:
    found = _producer_codes()
    assert {"ambiguous-space", "invalid-argument", "unknown-space"} <= set(found)
    offenders = sorted(set(found) - LEGACY_KEBAB_CODES)
    assert not offenders, (
        "new lowercase-kebab error codes must be frozen explicitly; found:\n"
        + "\n".join(
            f"{code}: {', '.join(found[code])}" for code in offenders
        )
    )


def _contract_code_schema(path: Path, *keys: str) -> dict[str, object]:
    import json

    node: object = json.loads(path.read_text(encoding="utf-8"))
    for key in keys:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, dict)
    return node


def test_both_contract_files_share_the_frozen_enum() -> None:
    schemas = [
        _contract_code_schema(
            ROOT / "contract" / "daemon-request" / "http-envelope.schema.json",
            "$defs",
            "error",
            "properties",
            "code",
        ),
        _contract_code_schema(
            ROOT / "contract" / "http-request" / "http-contract.v1.json",
            "errorEnvelope",
            "properties",
            "code",
        ),
    ]
    for schema in schemas:
        branches = schema["anyOf"]
        assert isinstance(branches, list) and len(branches) == 2
        assert branches[0] == {"pattern": "^[A-Z][A-Z0-9_]*$"}
        assert set(branches[1]["enum"]) == LEGACY_KEBAB_CODES


def test_the_contract_document_registers_every_exception() -> None:
    document = (ROOT / "contract" / "daemon-request" / "README.md").read_text(
        encoding="utf-8"
    )
    for code in LEGACY_KEBAB_CODES:
        uppercase = code.upper().replace("-", "_")
        assert f"`{code}`" in document, code
        assert f"`{uppercase}`" in document, code
