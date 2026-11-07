from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.kernel.impl.platform.file_lock import lock_exclusive, unlock

_ROOT = Path(__file__).resolve().parents[3]
_CHILD = """
import os
import sys

from hyprial.kernel.impl.platform.file_lock import lock_exclusive, unlock

descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    try:
        lock_exclusive(descriptor, blocking=False)
    except BlockingIOError:
        print("blocked")
    else:
        print("acquired")
        unlock(descriptor)
finally:
    os.close(descriptor)
"""


def _child_lock_state(path: Path) -> str:
    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else os.pathsep.join((source, inherited))
    )
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(path)],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_exclusive_lock_reports_contention_and_unlocks(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    first = os.open(path, os.O_RDWR)
    second = os.open(path, os.O_RDWR)
    try:
        lock_exclusive(first)
        with pytest.raises(BlockingIOError):
            lock_exclusive(second, blocking=False)

        unlock(first)
        lock_exclusive(second, blocking=False)
        unlock(second)
    finally:
        os.close(second)
        os.close(first)


def test_exclusive_lock_releases_when_owner_descriptor_closes(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    owner = os.open(path, os.O_RDWR)
    contender = os.open(path, os.O_RDWR)
    try:
        lock_exclusive(owner)
        os.close(owner)
        owner = -1

        lock_exclusive(contender, blocking=False)
        unlock(contender)
    finally:
        if owner >= 0:
            os.close(owner)
        os.close(contender)


def test_exclusive_lock_excludes_a_separate_process(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    owner = os.open(path, os.O_RDWR)
    try:
        lock_exclusive(owner)
        assert _child_lock_state(path) == "blocked"

        unlock(owner)
        assert _child_lock_state(path) == "acquired"
    finally:
        os.close(owner)
