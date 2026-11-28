"""Every contract/e2e script loads by path, standing on its own.

The CI baseline tests load scenario scripts with ``spec_from_file_location``,
which does NOT put the script's directory on ``sys.path``.  #733 added bare
``from _daemon_runner import ...`` lines to three scripts and dev went red
(task 25028, ModuleNotFoundError) -- while this author's own run was green,
because another test file had already put the directory on ``sys.path`` in the
same process.

So each script is loaded here in a FRESH interpreter (clean ``sys.path``, cwd
outside the repo): no other test's import side effects can mask a script that
cannot find its sibling module.

Decision (2026-11-26): three scripts import a POSIX-only module at module
level (``fcntl`` or ``pty``) and can never load on Windows.  Their parameters
carry the ``posix_only`` mark, so on Windows they are reported as skips that
name the module instead of failures; every other script is unmarked and still
loads there, reporting its real outcome.  The marks are listed in
``POSIX_ONLY_SCRIPTS`` and every name in it must be an existing script.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parents[5] / 'contract/e2e'
SCRIPTS = sorted(SCENARIOS.glob("*.py"))
POSIX_ONLY_SCRIPTS = {
    "with-machine-lock.py": "fcntl",
    "run-lark-app-onboarding.py": "pty",
    "run-real-harness-loopback.py": "pty",
}


def _script_parameter(path: Path) -> object:
    """Wrap a POSIX-only script's parameter with its ``posix_only`` mark.

    A script in ``POSIX_ONLY_SCRIPTS`` imports a POSIX-only module at module
    level, so it can never load on Windows; the mark keeps that script's
    parameter visible in the results as a skip that names the module, while
    every other script still loads and reports its real outcome there.
    """

    need = POSIX_ONLY_SCRIPTS.get(path.name)
    if need is None:
        return path
    return pytest.param(path, marks=pytest.mark.posix_only(need))


_LOADER = """
import importlib.util, sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("scenario_under_test", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
try:
    spec.loader.exec_module(module)
except SystemExit:
    pass
except ImportError as error:
    print(f"IMPORTERROR {error}")
    sys.exit(3)
"""


def test_the_scan_found_the_scenarios() -> None:
    """A silent empty parametrization would pass every time."""

    assert len(SCRIPTS) > 20, SCRIPTS


@pytest.mark.parametrize("script", [_script_parameter(path) for path in SCRIPTS], ids=lambda path: path.name)
def test_a_scenario_script_loads_by_path_without_import_errors(
    script: Path, tmp_path: Path
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _LOADER, str(script)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode != 3, (
        f"{script.name} cannot import a sibling module when loaded by path "
        f"(add sys.path.insert(0, str(Path(__file__).resolve().parent)) before "
        f"the import): {completed.stdout.strip()}"
    )
