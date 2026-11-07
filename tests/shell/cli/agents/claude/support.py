from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture(autouse=True)
def _legacy_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """These pre-P22 cases pin the unconfigured interactive mode."""

    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_kwargs: None)


@pytest.fixture(autouse=True)
def _initialized_command_home(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir(mode=0o700)
