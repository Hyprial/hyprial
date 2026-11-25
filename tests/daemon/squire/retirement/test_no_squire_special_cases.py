"""Squire is an ordinary agent (Allen 2026-11-03, phase B S10): retired special cases.

PIN guards.  Each scans product code for a squire branch that the ruling
retires; each carries a synthetic red case so the scan itself is proven.
"""

from __future__ import annotations

import ast
from pathlib import Path

import hyprial

SOURCE = Path(hyprial.__file__).parent
PROVIDER_GRANT_FILES = (
    "daemon/impl/application/actors/agents_admin/provider_credential.py",
    "daemon/impl/harnesses/model_provider.py",
    "shell/impl/cli/commands/agent/provider/grant_key.py",
)


def _squire_branches(source: str) -> list[int]:
    """Lines of comparisons or membership tests against a squire literal."""

    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if any(
            isinstance(operand, ast.Constant)
            and isinstance(operand.value, str)
            and "squire" in operand.value.lower()
            for operand in operands
        ):
            lines.append(node.lineno)
    return lines


def test_the_scan_finds_a_synthetic_squire_branch() -> None:
    assert _squire_branches("if agent.name == 'squire':\n    grant()\n") == [1]
    assert _squire_branches("if 'squire-container' in kinds:\n    pass\n") == [1]


def test_provider_key_grants_have_no_squire_special_case() -> None:
    found = {
        relative: _squire_branches((SOURCE / relative).read_text(encoding="utf-8"))
        for relative in PROVIDER_GRANT_FILES
    }

    assert found == {relative: [] for relative in PROVIDER_GRANT_FILES}


RETIRED_OWNER_DM_NAMES = (
    "send_owner_dm",
    "SendOwnerDm",
    "owner_notice_sender",
    "TARGET_SQUIRE_UNCONFIGURED",
    "TARGET_SQUIRE_ADAPTER_UNAVAILABLE",
)


def _retired_names(source: str) -> set[str]:
    """Retired owner-DM names defined, imported or referenced in *source*."""

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.alias):
            name = node.asname or node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "squire.user." in node.value:
                found.add("squire.user.")
            name = node.value
        else:
            continue
        found.update(
            retired for retired in RETIRED_OWNER_DM_NAMES if name.startswith(retired)
        )
    return found


def test_the_owner_dm_scan_finds_a_synthetic_port() -> None:
    assert _retired_names("def send_owner_dm_settled(self):\n    pass\n") == {
        "send_owner_dm"
    }
    assert _retired_names("x = ipc_errors.TARGET_SQUIRE_UNCONFIGURED\n") == {
        "TARGET_SQUIRE_UNCONFIGURED"
    }
    assert _retired_names("code = 'TARGET_SQUIRE_ADAPTER_UNAVAILABLE'\n") == {
        "TARGET_SQUIRE_ADAPTER_UNAVAILABLE"
    }
    assert _retired_names("path = 'squire.user.workflow'\n") == {"squire.user."}


def test_no_owner_dm_port_or_squire_code_remains_in_product_code() -> None:
    offenders = {
        str(path.relative_to(SOURCE)): sorted(found)
        for path in sorted(SOURCE.rglob("*.py"))
        if (found := _retired_names(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_user_wake_paths_carry_no_squire_prefix() -> None:
    from hyprial.daemon.impl.application.messaging.delivery import wake

    assert wake.WAKE_PATH_PREFIX == "user."
