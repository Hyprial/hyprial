"""The route rejection text must read the shared address-family table."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from hyprial.kernel.impl.primitives import uri as uri_module

ROOT = Path(__file__).resolve().parents[4]
URI_PATH = ROOT / "src" / "hyprial" / "kernel" / "impl" / "primitives" / "uri.py"


def test_route_rejection_uses_the_address_family_table() -> None:
    tree = ast.parse(URI_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "delivery_address_error"
    )
    names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert "ADDRESS_FAMILIES" in names
    docstring = (
        function.body[0].value
        if function.body and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
        else None
    )
    assert not any(
        isinstance(node, ast.Constant)
        and node is not docstring
        and isinstance(node.value, str)
        and "route:<adapter>:<route>" in node.value
        for node in ast.walk(function)
    )


def test_table_is_the_single_family_surface() -> None:
    families = uri_module.ADDRESS_FAMILIES
    assert tuple(families) == ("agent", "user", "route")
    assert all(family.name == key for key, family in families.items())
    assert not families["route"].escalation_supported
    assert families["route"].grammar == "route:<adapter>:<route>"
    assert families["route"].setup


def test_rejection_text_changes_when_the_table_entry_changes(monkeypatch) -> None:
    """A module-level literal cannot hide behind a name reference."""

    changed = replace(
        uri_module.ADDRESS_FAMILIES["route"],
        usage="SENTINEL_USAGE",
        setup="SENTINEL_SETUP",
    )
    monkeypatch.setitem(uri_module.ADDRESS_FAMILIES, "route", changed)
    message = uri_module.delivery_address_error("route:adapter:route")
    assert message is not None
    assert "SENTINEL_USAGE" in message
    assert "SENTINEL_SETUP" in message
