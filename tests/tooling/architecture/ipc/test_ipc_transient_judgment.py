"PR #332 F4② tripwire: no second transient-judgment system.\n\nThe ruling (spec-pr332-fixes §F4 ②, Allen 2026-09-03): transiency is judged\nby ``except TransientDaemonError`` (or a registered subclass) -- one\nhierarchy in contracts/ipc_errors.py, one registration point, both ends.\nAny of the following reintroduces a SECOND judgment system and turns this\nfile red:\n\n1. a string literal of a registered transient code anywhere in product\n   code (``src/hyprial`` or ``contract/e2e``) outside the registry\n   module -- the old ``TRANSIENT_DAEMON_CODES`` tuple shape included;\n2. a comparison against ``<expr>.code`` that mentions a transient code\n   (literal or registry constant) -- the old\n   ``if error.code != ipc_errors.DAEMON_RESTORING`` shape;\n3. minting a transient code through a GENERIC error constructor\n   (``DaemonRequestError`` / ``CliError`` / ``DaemonRequestRejected``)\n   instead of the registered subclass -- the shape the daemon's restore\n   gate used before the mint became the class.\n\nScope: ``src/hyprial`` and ``contract/e2e`` (product code and the\nfourth client, the e2e runner).  ``tests/`` and ``contract/cli-blackbox``\nare assertion surfaces -- they verify rendered codes and are allowed to\nspell them, exactly as ``tests/daemon/ipc/error/test_ipc_error_code_registry.py`` allows.\n"

from __future__ import annotations

import ast
from pathlib import Path

from hyprial.kernel import ipc_errors

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'
CONTRACT_SCENARIOS = Path(__file__).resolve().parents[4] / 'contract/e2e'
REGISTRY_FILE = SRC / "kernel" / "impl" / "contracts" / "daemon" / "ipc_errors.py"

TRANSIENT_CODES = frozenset(ipc_errors.TRANSIENT_CODE_CLASSES)
TRANSIENT_CONSTANT_NAMES = frozenset(TRANSIENT_CODES)  # constants are named like values
GENERIC_ERROR_CONSTRUCTORS = frozenset(
    {"DaemonRequestError", "CliError", "DaemonRequestRejected"}
)


def _scanned_files() -> list[Path]:
    files = [path for path in sorted(SRC.rglob("*.py")) if path != REGISTRY_FILE]
    files.extend(sorted(CONTRACT_SCENARIOS.glob("*.py")))
    return files


def _mentions_transient_code(node: ast.AST) -> bool:
    """Does this expression spell out a transient code (literal or constant)?"""

    if isinstance(node, ast.Constant) and node.value in TRANSIENT_CODES:
        return True
    if isinstance(node, ast.Attribute) and node.attr in TRANSIENT_CONSTANT_NAMES:
        return True
    if isinstance(node, ast.Name) and node.id in TRANSIENT_CONSTANT_NAMES:
        return True
    return False


def _is_code_attribute(node: ast.AST) -> bool:
    """Is this expression a ``<expr>.code`` read (a code comparison site)?"""

    return isinstance(node, ast.Attribute) and node.attr == "code"


def test_no_transient_code_literal_outside_the_registry() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in TRANSIENT_CODES
            ):
                offenders.append(f"{path}: {node.lineno}: {node.value!r}")
    assert not offenders, (
        "registered transient codes must never be spelled as string "
        "literals outside contracts/ipc_errors.py (use the registered "
        "class or the registry constant):\n" + "\n".join(offenders)
    )


def test_no_transient_code_comparison_against_dot_code() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands: list[ast.AST] = [node.left, *node.comparators]
            if not any(_is_code_attribute(operand) for operand in operands):
                continue
            if any(_mentions_transient_code(operand) for operand in operands):
                offenders.append(f"{path}: {node.lineno}")
    assert not offenders, (
        "transiency is judged with `except TransientDaemonError` (or a "
        "registered subclass), never by comparing `.code` against a "
        "transient code:\n" + "\n".join(offenders)
    )


def test_no_transient_code_minted_through_a_generic_error_constructor() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else None)
            )
            if name not in GENERIC_ERROR_CONSTRUCTORS:
                continue
            if node.args and _mentions_transient_code(node.args[0]):
                offenders.append(f"{path}: {node.lineno}: {name}")
    assert not offenders, (
        "transient codes are minted by raising their registered subclass, "
        "not by constructing a generic error with the code:\n" + "\n".join(offenders)
    )
