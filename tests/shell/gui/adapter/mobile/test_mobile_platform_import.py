"""The App Core host must import where POSIX ``fcntl`` does not exist.

``adapter/host.py`` imports the mobile wiring unconditionally so the local
Core can be served without the remote facade.  A module-scope ``fcntl`` import
inside the mobile package therefore takes down the whole host on Windows
before it can bind.  The probe denies ``fcntl`` only to modules in the mobile
package, reproducing that import failure on any OS; the release check still
imports the host with the real Windows packaged interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[5]
_PROBE = """
import builtins

_real_import = builtins.__import__


def _import_without_posix_fcntl(
    name, globals=None, locals=None, fromlist=(), level=0
):
    module = "" if globals is None else str(globals.get("__name__", ""))
    if name == "fcntl" and module.startswith(
        "hyprial.shell.impl.gui.adapter.mobile"
    ):
        raise ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
    return _real_import(name, globals, locals, fromlist, level)


builtins.__import__ = _import_without_posix_fcntl
import hyprial.shell.impl.gui.adapter.host
print("imported")
"""


def test_host_imports_without_posix_fcntl_in_the_mobile_package() -> None:
    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else os.pathsep.join((source, inherited))
    )
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "imported"
