"""The dev-upgrade smoke package imports without the signals it stops a child with.

``scripts/ops/_dev_upgrade_smoke/gui.py`` reads the numbers of SIGTERM and SIGKILL
when it stops a child, not when it is imported.  The ``signal`` module of
Windows has no SIGKILL, and an import-time lookup raised ``AttributeError``
there: the import of ``gui`` failed, and with it the import of every test
module that loads the package, so none of them could be collected.

``tests/tooling/ops/gui/test_gui_child.py`` repeated it in the table of its
stop-timeout cases: a ``parametrize`` table is evaluated when the module is
imported, so the SIGKILL it read there failed the import of the test module
itself.  The last case imports that module the same way, because fixing the
package does not keep its tests from repeating the mistake.  It imports the
module as a plain module: no ``conftest`` is loaded and no test in it runs,
since what fails is the import.  Both modules need nothing beyond ``pytest``.

Decisions:
- Each case starts a fresh interpreter with ``-I``, deletes one attribute from
  its ``signal`` module and imports the package, or the test module, there.
  It has to be a fresh interpreter: the package is imported once per process
  and cached, so an in-process check cannot tell whether the import itself
  touched the signal, and the old code passes it.
- This module lives outside ``tests/tooling/ops/``: the autouse fixture of
  that directory's ``conftest.py`` replaces ``subprocess.Popen`` so that no
  smoke test starts a real process, and it is inherited by every subdirectory.
  Nothing here changes that fixture.
- The package is loaded from ``scripts/ops`` by path, as the smoke tests do,
  so no child interpreter needs an installed ``hyprial``.  The children of
  the first two cases need no test extras either; the child of the third
  imports a test module, which imports ``pytest``, so that case needs the
  ``test`` extra.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]

IMPORT_WITHOUT_SIGNALS = r"""
import json, signal, sys
for name in sys.argv[2:]:
    if hasattr(signal, name):
        delattr(signal, name)
sys.path.insert(0, sys.argv[1])
try:
    import _dev_upgrade_smoke.gui
except AttributeError as error:
    print(json.dumps({"imported": False, "error": str(error)}))
else:
    print(json.dumps({"imported": True, "steps": [
        [step, name, seconds] for step, name, seconds in
        _dev_upgrade_smoke.gui.GUI_STOP_STEPS
    ]}))
"""


def _import_gui_without(*names: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            IMPORT_WITHOUT_SIGNALS,
            str(ROOT / "scripts" / "ops"),
            *names,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


IMPORT_TEST_MODULE_WITHOUT_SIGNALS = r"""
import importlib, json, os, signal, sys, traceback
root, module = sys.argv[1], sys.argv[2]
for name in sys.argv[3:]:
    if hasattr(signal, name):
        delattr(signal, name)
sys.path[:0] = [os.path.join(root, "scripts", "ops"), root]
try:
    importlib.import_module(module)
except AttributeError as error:
    frame = traceback.extract_tb(error.__traceback__)[-1]
    print(json.dumps({"imported": False, "error": str(error),
                      "file": os.path.relpath(frame.filename, root), "line": frame.lineno}))
else:
    print(json.dumps({"imported": True}))
"""


def _import_test_module_without(module: str, *names: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", IMPORT_TEST_MODULE_WITHOUT_SIGNALS, str(ROOT), module, *names],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", ["SIGKILL", "SIGTERM"])
def test_the_package_imports_without_a_stop_signal(name: str) -> None:
    outcome = _import_gui_without(name)

    assert outcome["imported"] is True, outcome
    assert outcome["steps"] == [
        ["stdin closed", None, 30.0],
        ["SIGTERM", "SIGTERM", 10.0],
        ["SIGKILL", "SIGKILL", 5.0],
    ]


def test_the_stop_cases_import_without_the_signals_they_expect() -> None:
    outcome = _import_test_module_without("tests.tooling.ops.gui.test_gui_child", "SIGKILL")

    assert outcome["imported"] is True, outcome
