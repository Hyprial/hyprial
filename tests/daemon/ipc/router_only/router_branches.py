"""Read the serving router's own method branches out of its source.

Decision: daemon-first-z4b-router-only-refusal-20261117.  The table
``ROUTER_ONLY_METHODS`` is only trustworthy while it equals what the router
branches answer.  This module derives the branch names from the source text of
the three dispatch functions, so the check sees the code and not a second list.
An operand it cannot resolve raises instead of being skipped.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Collection, Mapping

from hyprial.daemon.impl.application.actors.agents_admin import (
    migration as agents_migration,
)
from hyprial.daemon.impl.application.actors.agents_admin import (
    registry_ops as agents_registry_ops,
)
from hyprial.daemon.impl.ipc import router as ipc_router

GATE_NAMES = frozenset({"METHOD_REGISTRY", "MIGRATED_METHODS"})
ALWAYS_SERVED = frozenset({"ping", "daemon.catalog", "shutdown"})
DISPATCH_FUNCTIONS = (
    (ipc_router, "handle"),
    (agents_registry_ops, "_handle_agent"),
    (agents_migration, "_handle_agent_migration"),
)


class UnresolvedOperand(AssertionError):
    """A comparison against ``method`` whose other side could not be resolved."""


def _resolve(
    node: ast.expr, namespace: Mapping[str, object], gates: Collection[str]
) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve(node.left, namespace, gates)
        right = _resolve(node.right, namespace, gates)
        return {a + b for a in left for b in right}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        names: set[str] = set()
        for element in node.elts:
            names |= _resolve(element, namespace, gates)
        return names
    if isinstance(node, ast.Name):
        if node.id in gates:
            return set()
        value = namespace.get(node.id)
        if isinstance(value, str):
            return {value}
        if isinstance(value, (set, frozenset, tuple, list)) and all(
            isinstance(item, str) for item in value
        ):
            return set(value)
    raise UnresolvedOperand(ast.dump(node))


def _is_method(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "method"


def branch_names(
    source: str,
    function: str,
    namespace: Mapping[str, object],
    gates: Collection[str] = GATE_NAMES,
) -> set[str]:
    """Every name a positive ``method ==`` / ``method in`` comparison can match."""

    tree = ast.parse(textwrap.dedent(source))
    targets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    ]
    if len(targets) != 1:
        raise UnresolvedOperand(f"{function}: {len(targets)} definitions")
    found: set[str] = set()
    for node in ast.walk(targets[0]):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(_is_method(operand) for operand in operands):
            continue
        if len(node.ops) != 1:
            raise UnresolvedOperand(ast.dump(node))
        if not isinstance(node.ops[0], (ast.Eq, ast.In)):
            continue
        other = node.comparators[0] if _is_method(node.left) else node.left
        found |= _resolve(other, namespace, gates)
    return found


def router_branch_names(overrides: Mapping[str, str] | None = None) -> set[str]:
    """Branch names of the real dispatch functions; ``overrides`` swaps a module's source."""

    names: set[str] = set()
    for module, function in DISPATCH_FUNCTIONS:
        source = (overrides or {}).get(module.__name__) or inspect.getsource(module)
        names |= branch_names(source, function, vars(module))
    return names


def disagreement(
    branches: Collection[str], registered: Collection[str], table: Collection[str]
) -> tuple[list[str], list[str]]:
    """Names the branches answer but the table lacks, and table names nothing answers."""

    expected = set(branches) - set(registered) - ALWAYS_SERVED
    return sorted(expected - set(table)), sorted(set(table) - expected)
