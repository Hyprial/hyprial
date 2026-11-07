"""The smolvm carrier must not break the daemon import path on Windows.

The Windows desktop payload starts ``hyprial.cli daemon run``. The harness
package reaches ``codex.client``, which imports the smolvm carrier at module
scope; the carrier only executes on Darwin/arm64 (``validate_materials``
refuses every other host), but its imports used to drag POSIX-only modules
(``fcntl``, ``pwd``) into every daemon start, which died with
``No module named 'fcntl'`` before touching a store.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]


def test_daemon_harness_import_path_needs_no_posix_only_modules():
    script = """
import sys

# The kernel lock module is OS-dispatched and may bind fcntl on this host; load
# it first so the simulation targets the carrier imports, not the lock itself.
import hyprial.kernel.impl.platform.file_lock  # noqa: F401
sys.modules["fcntl"] = None  # Windows: POSIX-only modules do not exist
sys.modules["pwd"] = None
import hyprial.daemon.impl.harnesses.codex.client  # noqa: F401
print("imported")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "imported"
