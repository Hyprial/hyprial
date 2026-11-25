"""Every test path and node id named in a CI workflow must exist.

Workflows name some gates explicitly (``path.py::test_name``) to keep them fast
and readable.  When a test is renamed or removed, such a gate does not fail at
review time: pytest collects nothing and exits 5 only on the PRs that take
that branch.  The docs-only gate carried a removed node id for days, so every
docs-only PR went red while dev's own CI never ran the step.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".forgejo" / "workflows"
_TARGET = re.compile(r"\btests/[A-Za-z0-9_/.-]+\.py(?:::[A-Za-z0-9_]+)?")


def workflow_test_targets(workflows: Path) -> dict[str, set[str]]:
    """Map each explicit ``tests/...py[::name]`` target to the workflows naming it."""

    targets: dict[str, set[str]] = {}
    for workflow in sorted(workflows.glob("*.y*ml")):
        for match in _TARGET.finditer(workflow.read_text(encoding="utf-8")):
            targets.setdefault(match.group(0), set()).add(workflow.name)
    return targets


def _top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def missing_targets(root: Path, targets: dict[str, set[str]]) -> list[str]:
    missing: list[str] = []
    for target, workflows in sorted(targets.items()):
        file_part, _, node = target.partition("::")
        path = root / file_part
        if not path.is_file():
            missing.append(f"{target} (file missing; named in {sorted(workflows)})")
        elif node and node not in _top_level_names(path):
            missing.append(f"{target} (no top-level {node}; named in {sorted(workflows)})")
    return missing


def test_every_workflow_test_target_exists() -> None:
    targets = workflow_test_targets(WORKFLOWS)
    # An empty scan must never read as clean: that is the silent-decay shape.
    assert len(targets) >= 10, f"suspiciously few workflow test targets: {targets}"
    assert missing_targets(ROOT, targets) == []


def test_a_renamed_node_id_is_reported(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests" / "gate"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_policy.py").write_text("def test_kept():\n    pass\n", "utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "ci.yml").write_text(
        "run: pytest tests/gate/test_policy.py::test_kept "
        "tests/gate/test_policy.py::test_renamed tests/gate/test_gone.py\n",
        "utf-8",
    )

    missing = missing_targets(tmp_path, workflow_test_targets(workflows))

    assert [entry.split(" ")[0] for entry in missing] == [
        "tests/gate/test_gone.py",
        "tests/gate/test_policy.py::test_renamed",
    ]
