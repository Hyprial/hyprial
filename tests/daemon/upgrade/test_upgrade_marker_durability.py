"""doctor --fix / upgrade markers stay durable without opening directories by hand.

#1902 reproduced ``PermissionError: [Errno 13]`` from ``hyprial doctor --fix`` on
native Windows: the upgrade-step marker writer and the mcp argv rewrite each
did ``os.open(path.parent, os.O_RDONLY)`` + ``os.fsync``.  Both now go through
the one kernel seam, so an OS that cannot open a directory skips the directory
flush instead of failing the step.  Simulated here by refusing directory opens.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from hyprial.daemon.impl.upgrade import runner
from hyprial.daemon.impl.upgrade.steps import mcp_entry
from hyprial.kernel.impl.platform.filesystem import durability


@pytest.fixture
def directories_cannot_be_opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_open = os.open

    def open_files_only(path: object, flags: int, *args: object, **kwargs: object) -> int:
        candidate = Path(os.fspath(path))
        if candidate.is_dir() and candidate.is_relative_to(tmp_path):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(durability.os, "open", open_files_only)


@pytest.mark.usefixtures("directories_cannot_be_opened")
def test_step_marker_is_written_where_directories_cannot_be_opened(
    tmp_path: Path,
) -> None:
    (tmp_path / runner.STEP_DIRNAME).mkdir()
    runner._write_marker(tmp_path, "probe-step", {"state": "done", "attempts": 1})
    marker = json.loads(
        (tmp_path / runner.STEP_DIRNAME / "probe-step.json").read_text(encoding="utf-8")
    )
    assert marker == {"state": "done", "attempts": 1}


@pytest.mark.usefixtures("directories_cannot_be_opened")
def test_mcp_argv_rewrite_is_written_where_directories_cannot_be_opened(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.json"
    mcp_entry._atomic_write(target, b'{"ok":true}\n')
    assert target.read_bytes() == b'{"ok":true}\n'
    assert not list(tmp_path.glob(".*tmp"))
