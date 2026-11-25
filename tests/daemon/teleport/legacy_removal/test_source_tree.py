"""Slice 7: the P2 / SSH / agent_home_chain code is gone and nothing imports it.

Decisions are recorded in ``support.py``.  The teleport closure check follows
the imports teleport and the primitives name explicitly, so the report points
at the teleport edge that still needs legacy code; implicit parent packages
(through which the whole daemon is reachable) are covered by the whole-tree
checks.  The kept capabilities are pinned by the function names teleport
uses, not by their location.
"""

from __future__ import annotations

from .support import (
    TELEPORT_SEEDS,
    chain,
    import_closure,
    legacy_modules,
    legacy_name,
    product_modules,
    raw_imports,
    resolve,
)

KEPT_FUNCTIONS = frozenset(
    {
        "export_bundle",
        "validate_bundle",
        "land_bundle_into_registry",
        "seal_credential_envelope",
        "ensure_recipient_key",
        "quiesce_agent",
    }
)


def test_no_legacy_module_remains_under_src() -> None:
    remaining = legacy_modules()
    assert remaining == {}, "legacy transfer code still present:\n" + "\n".join(
        f"  {name}: {', '.join(reasons)}" for name, reasons in sorted(remaining.items())
    )


def test_no_module_imports_or_exports_legacy_code() -> None:
    legacy = set(legacy_modules())
    edges: list[str] = []
    for name, module in sorted(product_modules().items()):
        if name in legacy:
            continue
        for raw in sorted(raw_imports(module)):
            target = resolve(raw)
            if legacy_name(raw) or target in legacy:
                edges.append(f"  {name} -> {raw}")
    assert edges == [], "modules still reference legacy transfer code:\n" + "\n".join(edges)


def test_teleport_and_primitives_import_graph_holds_no_legacy_code() -> None:
    parent = import_closure(TELEPORT_SEEDS)
    legacy = legacy_modules()
    reached = sorted(name for name in parent if name in legacy or legacy_name(name))
    assert reached == [], "teleport reaches legacy transfer code:\n" + "\n".join(
        f"  {chain(parent, name)}" for name in reached
    )


def test_teleport_still_reaches_the_kept_capabilities() -> None:
    modules = product_modules()
    defined: set[str] = set()
    for name in import_closure(TELEPORT_SEEDS):
        defined |= modules[name].functions
    assert KEPT_FUNCTIONS <= defined, sorted(KEPT_FUNCTIONS - defined)
