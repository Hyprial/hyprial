"""Isolated daemon fixture for the P2 config rebind recovery suites."""

from __future__ import annotations

from pathlib import Path

import pytest

import hyprial.daemon.impl.harnesses.runtime.launcher as launcher_module

from .config_rebind_support import (
    StubPiProcess,
    close_application,
    new_application,
)


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    StubPiProcess.instances = []
    monkeypatch.setattr(launcher_module, "PiRpcProcess", StubPiProcess)
    app = new_application(tmp_path)
    try:
        yield app
    finally:
        close_application(app)
