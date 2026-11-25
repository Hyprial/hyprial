"""Executable contract for the incremental pyright Windows-attribute gate.

``scripts/ci/check_pyright_windows.py`` runs pyright with ``--pythonplatform
Windows`` on the product files a change touches, keeps only Windows-missing
attributes of ``os``/``signal``/``fcntl``/``socket``/``select``/``pwd``/
``termios``/``resource``, and fails when a ``(path, module, attribute)`` count
rises against the same files at the base.  A fake pyright (same CLI, same
``--outputjson`` shape) stands in for the real one here.
"""

from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ci" / "check_pyright_windows.py"
SPEC = importlib.util.spec_from_file_location("check_pyright_windows", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

FAKE_PYRIGHT = '''
import json, re, sys
from pathlib import Path
args = sys.argv[1:]
if args[args.index("--pythonplatform") + 1] != "Windows" or "--outputjson" not in args:
    sys.exit(4)
files = [a for a in args if a.endswith(".py")]
diagnostics = []
for name in files:
    path = Path(name).resolve()
    for number, line in enumerate(path.read_text().splitlines()):
        for module, attr in re.findall(r"\\b(os|json|signal)\\.(getuid|fork|loads|SIGKILL)\\b", line):
            diagnostics.append({"file": str(path), "severity": "error", "rule": "reportAttributeAccessIssue",
                                "message": f'"{attr}" is not a known attribute of module "{module}"',
                                "range": {"start": {"line": number, "character": 0}}})
        if "maybe" in line:
            diagnostics.append({"file": str(path), "severity": "error", "rule": "reportOptionalMemberAccess",
                                "message": '"getuid" is not a known attribute of "None"',
                                "range": {"start": {"line": number, "character": 0}}})
print(json.dumps({"version": "fake", "generalDiagnostics": diagnostics}))
sys.exit(1 if diagnostics else 0)
'''
A = "src/hyprial/daemon/impl/probe.py"
B = "src/hyprial/daemon/impl/added.py"
SEAM = "src/hyprial/kernel/impl/manysys/probe.py"
OLD_PLATFORM = "src/hyprial/kernel/impl/platform/probe.py"
OLD_OWNED_PROCESS = "src/hyprial/kernel/impl/processes/owned_process.py"


def _git(root: Path, *args: str) -> str:
    command = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, {A: "import os\nos.getuid()\n", SEAM: "import os\nos.fork()\n", "README": "x\n"})
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "tag", "base")
    return root


def _fake(tmp_path: Path) -> str:
    fake = tmp_path / "fake_pyright.py"
    fake.write_text(FAKE_PYRIGHT, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))}"


def _head(root: Path, files: dict[str, str]) -> None:
    _write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")


def test_a_new_windows_missing_attribute_in_a_changed_file_is_red(repo: Path, tmp_path: Path) -> None:
    _head(repo, {A: "import os\nos.getuid()\nos.getuid()\n"})
    report = GATE.check(repo, "base", pyright=_fake(tmp_path))
    assert report["status"] == "FAIL"
    assert report["files"] == [A]
    assert report["newFindings"] == [{"path": A, "symbol": "os.getuid", "count": 2, "base": 1, "lines": [2, 3]}]


def test_moved_lines_unrelated_diagnostics_and_fixes_pass(repo: Path, tmp_path: Path) -> None:
    _head(repo, {A: "import os, json\n\n\nos.getuid()\njson.loads('1')\nmaybe.getuid()\n"})
    report = GATE.check(repo, "base", pyright=_fake(tmp_path))
    assert (report["status"], report["headFindings"], report["baseFindings"]) == ("PASS", 1, 1)
    _head(repo, {A: "import os\n"})
    assert GATE.check(repo, "base", pyright=_fake(tmp_path))["status"] == "PASS"


def test_a_new_file_counts_from_zero_and_only_manysys_is_skipped(repo: Path, tmp_path: Path) -> None:
    assert GATE.PLATFORM_SEAMS == ("src/hyprial/kernel/impl/manysys/",)
    assert not GATE.checked(SEAM)
    assert GATE.checked(OLD_PLATFORM)
    assert GATE.checked(OLD_OWNED_PROCESS)
    _head(repo, {B: "import signal\nsignal.SIGKILL\n", SEAM: "import os\nos.fork()\nos.fork()\n"})
    report = GATE.check(repo, "base", pyright=_fake(tmp_path))
    assert report["files"] == [B]
    assert report["newFindings"] == [{"path": B, "symbol": "signal.SIGKILL", "count": 1, "base": 0, "lines": [2]}]


def test_the_base_is_checked_in_its_own_snapshot(repo: Path, tmp_path: Path) -> None:
    _head(repo, {A: "import os\nos.getuid()\n# head only\n"})
    report = GATE.check(repo, "base", pyright=_fake(tmp_path))
    assert (report["status"], report["baseMode"], report["baseFindings"]) == ("PASS", "merge-base", 1)
    assert report["base"] == _git(repo, "rev-parse", "base")


def test_no_changed_product_file_needs_no_pyright(repo: Path) -> None:
    _head(repo, {"README": "y\n"})
    report = GATE.check(repo, "base", pyright="/nonexistent/pyright")
    assert (report["status"], report["files"]) == ("PASS", [])


@pytest.mark.parametrize(
    ("pyright", "error"),
    [
        ("/nonexistent/pyright", "cannot run pyright"),
        (f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(3)'", "pyright exited 3"),
        (f"{shlex.quote(sys.executable)} -c 'print(1)'", "not a JSON report"),
        (f"{shlex.quote(sys.executable)} -c 'print(\"{{\")'", "not JSON"),
    ],
)
def test_pyright_failing_to_run_fails_closed(repo: Path, pyright: str, error: str) -> None:
    _head(repo, {A: "import os\nos.getuid()\nos.getuid()\n"})
    report = GATE.check(repo, "base", pyright=pyright)
    assert report["status"] == "FAIL"
    assert error in report["errors"][0]


def test_an_unknown_base_fails_closed(repo: Path, tmp_path: Path) -> None:
    report = GATE.check(repo, "no-such-ref", pyright=_fake(tmp_path))
    assert report["status"] == "FAIL" and "not available" in report["errors"][0]


def test_only_windows_missing_stdlib_attributes_are_kept() -> None:
    cwd = Path("/work")
    report = {"generalDiagnostics": [
        {"file": "/work/src/hyprial/a.py", "rule": "reportAttributeAccessIssue",
         "message": '"flock" is not a known attribute of module "fcntl"', "range": {"start": {"line": 3}}},
        {"file": "/work/src/hyprial/a.py", "rule": "reportAttributeAccessIssue",
         "message": '"AF_UNIX" is not a known attribute of module "socket"', "range": {"start": {"line": 5}}},
        {"file": "/work/src/hyprial/a.py", "rule": "reportAttributeAccessIssue",
         "message": '"loads" is not a known attribute of module "json"', "range": {"start": {"line": 6}}},
        {"file": "/work/src/hyprial/a.py", "rule": "reportOptionalMemberAccess",
         "message": '"getuid" is not a known attribute of "None"', "range": {"start": {"line": 7}}},
        {"file": "/work/src/hyprial/a.py", "rule": "reportAttributeAccessIssue",
         "message": 'Cannot access attribute "x" for class "Foo"', "range": {"start": {"line": 8}}},
    ]}
    found = GATE.windows_attribute_findings(report, cwd)
    assert [(row["module"], row["attribute"], row["line"]) for row in found] == [("fcntl", "flock", 4),
                                                                                ("socket", "AF_UNIX", 6)]
    assert {row["path"] for row in found} == {"src/hyprial/a.py"}


def test_the_gate_pins_pyright_without_a_lock_entry_and_runs_in_gate_python() -> None:
    assert GATE.DEFAULT_PYRIGHT.startswith("uvx --from pyright==")
    assert "pyright" not in (ROOT / "uv.lock").read_text(encoding="utf-8")
    workflow = (ROOT / ".forgejo" / "workflows" / "gate-python.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  cli-help-inventory:\n", 1)[1].split("\n  tailcat-go:\n", 1)[0]
    assert "scripts/ci/check_windows_portability.py" in job
    assert "scripts/ci/check_pyright_windows.py \\\n            --base-ref \"$pyright_base\"" in job
