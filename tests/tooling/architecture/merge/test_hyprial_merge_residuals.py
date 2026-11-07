"""The merge-result old-token inventory must be exhaustive and fail closed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CHECK = ROOT / "scripts" / "verify" / "check_hyprial_merge_residuals.py"


def test_merged_src_and_tests_have_no_unclassified_old_product_tokens() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHECK)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = completed.stdout.splitlines()[-1]
    assert "parse_errors=0" in summary
    assert "unclassified=0" in summary
    # The rename is complete: no old product token remains at all.
    assert "entries=0" in summary
    assert "scanned_files=0" not in summary


def _guard_module():
    spec = importlib.util.spec_from_file_location("_residual_guard", CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass(slots=True)`` rebuilds the class and looks itself up in
    # ``sys.modules`` to do it, so registering before exec is required, not
    # tidiness: without it the import dies with a bare AttributeError from
    # dataclasses.py that says nothing about the cause.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_path_scoped_allowances_reference_existing_scanned_files() -> None:
    """A moved contract cannot leave a silent, dead path-scoped allowance."""
    guard = _guard_module()
    scoped_paths = sorted(
        {allowance.path for allowance in guard.ALLOWANCES if allowance.path is not None}
    )
    scanned, _ = guard._tracked_scope(None)

    assert set(scoped_paths) <= set(scanned)
    assert all((ROOT / relative).is_file() for relative in scoped_paths)


def test_literals_helper_cannot_express_a_family() -> None:
    """``_literals`` is the mechanism that makes "no shapes" structural.

    If it ever stops escaping, the rule degrades silently: the table still
    reads as a list of names while behaving as a list of patterns.
    """
    guard = _guard_module()
    pattern = guard._literals("a.b")
    assert pattern.search("a.b")
    assert not pattern.search("axb"), "'.' was treated as a wildcard"


def test_monorepo_scope_excludes_only_the_gui_component(monkeypatch, tmp_path) -> None:
    guard = _guard_module()
    listed = ["src/gui/image.png", "src/gui/plugin.js", "src/hyprial/cli.py",
              "src/future/cli.py", "src/gui_extra/cli.py", "tests/test_example.py"]
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    for relative in listed:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="\0".join(listed), stderr="")
    monkeypatch.setattr(guard.subprocess, "run", run)
    paths, _ = guard._tracked_scope(None)
    assert paths == sorted(listed[2:])


def _workspace_guard(tmp_path, monkeypatch):
    """Use a real isolated Git index without committing repository source."""
    guard = _guard_module()
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    tracked = 'tests/test_clean.py'
    path = tmp_path / tracked
    path.parent.mkdir(parents=True)
    path.write_text("# hyprial\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", tracked], cwd=tmp_path, check=True)
    return guard, tracked


def test_unknown_untracked_native_source_is_scanned_and_rejected(tmp_path, monkeypatch, capsys):
    guard, _ = _workspace_guard(tmp_path, monkeypatch)
    relative = "src/hyprial/daemon/impl/new_owner.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(f"# {guard.OLD}.never-considered/v1\n", encoding="utf-8")

    assert guard.main([]) == 1
    output = capsys.readouterr().out
    assert f"UNCLASSIFIED {relative}:1:" in output
    assert "scanned_files=2 decoded_files=2" in output
    assert "unclassified=1" in output


def test_deleted_tracked_source_is_not_worktree_scope(tmp_path, monkeypatch):
    guard, tracked = _workspace_guard(tmp_path, monkeypatch)
    (tmp_path / tracked).unlink()

    paths, _ = guard._tracked_scope(None)
    assert paths == []


def test_ignored_untracked_source_is_not_worktree_scope(tmp_path, monkeypatch, capsys):
    guard, tracked = _workspace_guard(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
    ignored = tmp_path / "src/ignored.py"
    ignored.parent.mkdir()
    ignored.write_text(f"# {guard.OLD}.never-considered/v1\n", encoding="utf-8")

    paths, _ = guard._tracked_scope(None)
    assert paths == [tracked]
    assert guard.main([]) == 0
    assert "scanned_files=1 decoded_files=1" in capsys.readouterr().out


def test_frozen_tree_scan_is_independent_of_worktree_changes(tmp_path, monkeypatch, capsys):
    guard, tracked = _workspace_guard(tmp_path, monkeypatch)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=tmp_path, text=True, capture_output=True, check=True
    ).stdout.strip()
    (tmp_path / tracked).unlink()
    unknown = tmp_path / "src/new.py"
    unknown.parent.mkdir()
    unknown.write_text(f"# {guard.OLD}.never-considered/v1\n", encoding="utf-8")

    paths, label = guard._tracked_scope(tree)
    assert paths == [tracked]
    assert label == tree
    assert guard.main(["--tree", tree]) == 0
    output = capsys.readouterr().out
    assert "scanned_files=1 decoded_files=1" in output
    assert "unclassified=0" in output
