"""Reverse test dependency contracts for standalone test selection."""

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUARDS = {
    "tests/kernel/language/test_exception_handler_bindings.py": "def test_guard(): pass\n",
    "tests/daemon/harnesses/actors/test_harness_restore_admission.py": "def test_guard(): pass\n",
    "tests/shell/compatibility/test_unreachable_surface_retirement.py": "def test_guard(): pass\n",
    "tests/tooling/architecture/rename/test_hyprial_rename_contract.py": "def test_guard(): pass\n",
    "tests/tooling/isolation/windows/test_windows_suite_harness.py": "def test_guard(): pass\n",
    "tests/tooling/refactor/scanning/test_c0_scan.py": "def test_guard(): pass\n",
}


def _impact():
    spec = importlib.util.spec_from_file_location(
        "pytest_impact_standalone", ROOT / "scripts/ci/pytest_impact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def fixture(tmp_path: Path) -> tuple[Path, str, str]:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Standalone fixture")
    files = {
        "tests/daemon/test_alpha.py": "def test_alpha(): pass\n",
        "tests/daemon/test_beta.py": "def test_beta(): pass\n",
        "tests/daemon/test_shared_cases.py": "CASES = (1, 2)\ndef test_cases(): pass\n",
        "tests/daemon/test_uses_shared.py": "from .test_shared_cases import CASES\ndef test_uses(): assert CASES\n",
        "tests/daemon/helper.py": "def build(): return 1\n",
        "tests/shell/test_cli.py": "def test_cli(): pass\n",
        "tests/integration/test_e2e.py": "def test_e2e(): pass\n",
        **GUARDS,
    }
    for path, content in files.items():
        write(tmp_path, path, content)
    base = commit(tmp_path, "base")
    git(tmp_path, "checkout", "-b", "pull-request")
    write(tmp_path, "tests/daemon/test_alpha.py", "def test_alpha(): assert True\n")
    head = commit(tmp_path, "head")
    git(tmp_path, "checkout", "main")
    git(tmp_path, "merge", "--no-ff", "pull-request", "-m", "merge")
    return tmp_path, base, head


def change(tmp_path: Path, path: str, content: str) -> tuple[Path, dict[str, object]]:
    root, _base, _head = fixture(tmp_path)
    git(root, "checkout", "-b", "second-change")
    write(root, path, content)
    head = commit(root, "change")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "second-change", "-m", "merge change")
    return root, {"pull_request": {"base": {"ref": "dev"}, "head": {"sha": head}}}


def test_a_changed_unreferenced_test_module_runs_alone(tmp_path):
    impact = _impact()
    root, event = change(tmp_path, "tests/daemon/test_alpha.py", "def test_alpha(): assert 1\n")
    selected = set(impact.plan(root, event, "pull_request")["tests"])
    assert "tests/daemon/test_alpha.py" in selected
    assert set(GUARDS) <= selected


def test_a_test_module_referenced_by_another_test_keeps_the_directory(tmp_path):
    impact = _impact()
    root, event = change(
        tmp_path,
        "tests/daemon/test_shared_cases.py",
        "CASES = (1, 2, 3)\ndef test_cases(): pass\n",
    )
    selected = set(impact.plan(root, event, "pull_request")["tests"])
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= selected
    assert "tests/daemon/test_uses_shared.py" in selected


def test_a_changed_helper_keeps_the_directory(tmp_path):
    impact = _impact()
    root, event = change(tmp_path, "tests/daemon/helper.py", "def build(): return 2\n")
    selected = set(impact.plan(root, event, "pull_request")["tests"])
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= selected


def test_a_changed_test_and_helper_take_the_wider_selection(tmp_path):
    impact = _impact()
    root, _base, _head = fixture(tmp_path)
    git(root, "checkout", "-b", "multi-change")
    write(root, "tests/daemon/test_alpha.py", "def test_alpha(): assert 1\n")
    write(root, "tests/daemon/helper.py", "def build(): return 3\n")
    head = commit(root, "multi change")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "multi-change", "-m", "merge multi")
    event = {"pull_request": {"base": {"ref": "dev"}, "head": {"sha": head}}}
    selected = set(impact.plan(root, event, "pull_request")["tests"])
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= selected


def test_an_unreadable_tree_cannot_select_a_module_alone(tmp_path, monkeypatch):
    impact = _impact()
    root, event = change(tmp_path, "tests/daemon/test_alpha.py", "def test_alpha(): pass\n")

    original_read_text = Path.read_text

    def refuse(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self.name.endswith(".py"):
            raise OSError("unreadable")
        return original_read_text(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "read_text", refuse)
    assert impact.plan(root, event, "pull_request")["mode"] == "full"
