"""E2E org prerequisites use the supported org lifecycle, not bare spaces."""

from __future__ import annotations

import ast
from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[3]
E2E = ROOT / "contract" / "e2e"


def _literal_prefix(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _bare_group_space_creations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 3:
            continue
        first = _literal_prefix(node.args[0])
        second = _literal_prefix(node.args[1])
        name = _literal_prefix(node.args[2])
        if first == "fs" and second == "create" and name and name.startswith("group-"):
            lines.append(node.lineno)
    return lines


def test_no_e2e_fixture_fabricates_an_org_as_a_bare_fs_space() -> None:
    offenders = {
        str(path.relative_to(ROOT)): lines
        for path in E2E.rglob("*.py")
        if (lines := _bare_group_space_creations(path))
    }
    assert offenders == {}


def test_shared_local_org_helper_returns_org_create_space_id() -> None:
    helper = runpy.run_path(str(E2E / "_runner_shared" / "org.py"))
    calls: list[tuple[str, ...]] = []

    class Daemon:
        def command(self, *args: str, **_kwargs: object) -> dict[str, object]:
            calls.append(args)
            if args[:2] == ("org", "create"):
                return {"ok": True, "org": args[2], "spaceId": "space-real-org"}
            if args[:3] == ("org", "acl", "grant"):
                return {"ok": True}
            if args[:3] == ("org", "fs", "mkdir"):
                return {"ok": True}
            raise AssertionError(args)

    result = helper["ensure_local_org"](
        Daemon(), "driver", prefix="fixture", timeout=0.1
    )

    assert result == "space-real-org"
    assert calls[0][:2] == ("org", "create")
    assert calls[0][2].startswith("group-fixture-")


def test_hosted_org_helper_returns_org_create_space_id() -> None:
    runner = runpy.run_path(str(E2E / "run-hosted-registry.py"))
    scenario_type = runner["Scenario"]
    scenario = scenario_type.__new__(scenario_type)
    calls: list[tuple[str, ...]] = []

    def cli(*args: str, **_kwargs: object) -> dict[str, object]:
        calls.append(args)
        if args[:2] == ("org", "create"):
            return {"ok": True, "org": args[2], "spaceId": "space-hosted-org"}
        if args[:3] == ("org", "acl", "grant"):
            return {"ok": True}
        if args[:3] == ("org", "fs", "mkdir"):
            return {"ok": True}
        raise AssertionError(args)

    scenario.cli = cli
    result = scenario.ensure_org_space("driver", prefix="hosted")

    assert result == "space-hosted-org"
    assert calls[0][:2] == ("org", "create")
    assert calls[0][2].startswith("group-hosted-")
