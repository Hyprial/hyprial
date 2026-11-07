"""The real guard must release package copies before the next test runs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from .guard_support import REPOSITORY


_DRIVER = r'''
import sys
import pytest

mode, basetemp, target = sys.argv[1:]

class Fault:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        patch = item._request.getfixturevalue("monkeypatch")
        if mode == "assertion-failure":
            def fail(*args, **kwargs):
                raise AssertionError("injected guard assertion")
            patch.setattr(item.module, "run_static_scan", fail)
        elif mode == "copy-failure":
            from tests.shell.cli.installation.restart.post_install import guard_support
            def fail_copy(source, destination, **kwargs):
                destination.mkdir(parents=True)
                (destination / "partial.py").write_text("partial copy")
                raise OSError("injected copy failure")
            patch.setattr(guard_support.shutil, "copytree", fail_copy)

raise SystemExit(pytest.main(
    [target, "-q", "--basetemp=" + basetemp], plugins=[Fault()]
))
'''


@pytest.mark.parametrize("mode", ["success", "assertion-failure", "copy-failure"])
def test_guard_workspace_is_removed_at_test_teardown(tmp_path: Path, mode: str) -> None:
    basetemp = tmp_path / "child-pytest"
    target = str(Path(__file__).with_name("test_post_install_fence.py")) + (
        "::test_static_whole_module_import_scan_has_no_findings"
    )
    environment = {**os.environ, "PYTEST_ADDOPTS": "", "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, "-c", _DRIVER, mode, str(basetemp), target],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == (0 if mode == "success" else 1), output
    if mode != "success":
        expected = "injected guard assertion" if mode == "assertion-failure" else "injected copy failure"
        assert expected in output, output
    # pytest retains its per-case directory. The large private copy (including
    # partial copies and subprocess-created state) must not remain inside it.
    retained = [path for path in basetemp.rglob("*") if path.is_file()]
    assert retained == [], f"guard retained {len(retained)} files: {retained[:5]}"
