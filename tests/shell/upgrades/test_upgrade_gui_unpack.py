"""``hyprial daemon upgrade`` records GUI retirement without failing daemon upgrades."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.gui import apps as gui_apps
from tests.shell.cli.installation.restart.support import (
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_upgrade_unpacks_the_paired_gui_after_installing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_upgrade(monkeypatch, tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(
        gui_apps,
        "upgrade",
        lambda home, **kwargs: calls.append(home)
        or {"ok": True, "version": "1.2.3", "upgraded": True},
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["upgraded"] is True
    assert payload["gui"] == {"ok": True, "version": "1.2.3", "upgraded": True}
    assert calls == [tmp_path / "home"]


def test_upgrade_records_gui_unavailable_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_upgrade(monkeypatch, tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["upgraded"] is True
    assert payload["gui"]["ok"] is False
    assert payload["gui"]["code"] == "GUI_UNAVAILABLE"
    assert "temporarily unavailable" in payload["gui"]["error"]
