"""The short ``/tmp`` root parent the ``smoke_bundle`` tests in this directory run under (AF_UNIX path limit), with a ``codesign`` stub on a PATH that has no ``/usr/sbin``; ``test_verify_daemon.py`` has its own ``parent`` fixture."""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.tooling.release.shell.daemon import fakes


@pytest.fixture()
def parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="smd-", dir="/tmp")).resolve()
    tools = directory / "tools"
    tools.mkdir()
    stub = tools / "codesign"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    (directory / "roots").mkdir()
    try:
        yield directory / "roots"
        fakes.reap(directory, tmp_path)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
