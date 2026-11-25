"""A changed test module nobody else refers to runs alone, not its whole directory.

Decision record:
- Queue time is the constraint (Allen 2026-11-14). The plan now selects the
  Python suite for a change to a test file, so the selection has to stay small:
  the changed module runs alone when no other file under tests/ mentions it.
- The check is textual and errs toward the wider selection. A module another
  test imports or loads by name, a helper that is not a test module, and a
  deleted module all keep the directory-level rule.
"""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]

BASE_FILES = {
    "src/hyprial/shell/cli_part.py": "VALUE = 1\n",
    "tests/daemon/test_alpha.py": "def test_alpha(): pass\n",
    "tests/daemon/test_beta.py": "def test_beta(): pass\n",
    "tests/daemon/test_shared_cases.py": "CASES = (1, 2)\ndef test_cases(): pass\n",
    "tests/daemon/test_uses_shared.py": "from .test_shared_cases import CASES\ndef test_uses(): assert CASES\n",
    "tests/daemon/helper.py": "def build(): return 1\n",
    "tests/daemon/__init__.py": "",
    "tests/shell/test_cli.py": "def test_cli(): pass\n",
    "tests/integration/test_e2e.py": "def test_e2e(): pass\n",
    "tests/tooling/isolation/test_teardown_guarantee.py": "def test_guard(): pass\n",
    "tests/kernel/addressing/test_address_parsing_guard.py": "def test_guard(): pass\n",
    "tests/identity/provider_keys/structure/test_public_seams.py": "def test_guard(): pass\n",
    "tests/scripts/test_actor_rustify_inventory.py": "def test_guard(): pass\n",
}


@pytest.fixture(scope="module")
def impact():
    spec = importlib.util.spec_from_file_location(
        "pytest_impact_standalone", ROOT / "scripts/ci/pytest_impact.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def write(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def commit(root):
    git(root, "add", "-A")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Standalone fixture")
    for path, content in BASE_FILES.items():
        write(tmp_path, path, content)
    return tmp_path, commit(tmp_path)


def plan_after(impact, repo, edits, *, removed=()):
    root, base = repo
    for path, content in edits.items():
        write(root, path, content)
    for path in removed:
        (root / path).unlink()
    payload = {"pull_request": {"base": {"sha": base, "ref": "dev"}, "head": {"sha": commit(root)}}}
    result = impact.plan(root, payload, "pull_request")
    assert result["mode"] == "selected", result
    return result["tests"]


def test_a_changed_module_nobody_refers_to_runs_alone(impact, repo):
    tests = plan_after(impact, repo, {"tests/daemon/test_alpha.py": "def test_alpha(): assert True\n"})
    assert tests == ["tests/daemon/test_alpha.py"]


def test_a_new_module_runs_alone(impact, repo):
    tests = plan_after(impact, repo, {"tests/daemon/test_gamma.py": "def test_gamma(): pass\n"})
    assert tests == ["tests/daemon/test_gamma.py"]


def test_a_module_another_test_imports_keeps_its_directory(impact, repo):
    tests = plan_after(
        impact, repo,
        {"tests/daemon/test_shared_cases.py": "CASES = (1, 2, 3)\ndef test_cases(): pass\n"},
    )
    assert "tests/daemon/test_uses_shared.py" in tests
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= set(tests)


def test_a_changed_helper_that_is_not_a_test_module_keeps_its_directory(impact, repo):
    tests = plan_after(impact, repo, {"tests/daemon/helper.py": "def build(): return 2\n"})
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= set(tests)


def test_two_standalone_modules_select_exactly_those_two(impact, repo):
    tests = plan_after(impact, repo, {
        "tests/daemon/test_alpha.py": "def test_alpha(): assert 1\n",
        "tests/shell/test_cli.py": "def test_cli(): assert 1\n",
    })
    assert tests == ["tests/daemon/test_alpha.py", "tests/shell/test_cli.py"]


def test_a_standalone_module_beside_a_helper_change_takes_the_wider_selection(impact, repo):
    tests = plan_after(impact, repo, {
        "tests/daemon/test_alpha.py": "def test_alpha(): assert 1\n",
        "tests/daemon/helper.py": "def build(): return 3\n",
    })
    assert {"tests/daemon/test_alpha.py", "tests/daemon/test_beta.py"} <= set(tests)


def test_a_product_change_still_selects_its_layers_beside_a_standalone_module(impact, repo):
    tests = set(plan_after(impact, repo, {
        "src/hyprial/shell/cli_part.py": "VALUE = 2\n",
        "tests/daemon/test_alpha.py": "def test_alpha(): assert 1\n",
    }))
    assert {"tests/daemon/test_alpha.py", "tests/shell/test_cli.py", "tests/integration/test_e2e.py"} <= tests
    assert "tests/daemon/test_beta.py" not in tests
    assert set(impact.CROSS_CUTTING_GUARDS) <= tests


def test_a_deleted_module_keeps_the_directory_rule(impact, repo):
    tests = plan_after(impact, repo, {}, removed=["tests/daemon/test_beta.py"])
    assert "tests/daemon/test_alpha.py" in tests and "tests/daemon/test_beta.py" not in tests


def test_an_unreadable_tree_selects_no_module_alone(impact, tmp_path, monkeypatch):
    write(tmp_path, "tests/a/test_one.py", "def test_one(): pass\n")
    write(tmp_path, "tests/a/test_two.py", "def test_two(): pass\n")

    def refuse(self, *args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", refuse)
    assert impact.standalone_test_modules(tmp_path, ["tests/a/test_one.py"]) == set()
