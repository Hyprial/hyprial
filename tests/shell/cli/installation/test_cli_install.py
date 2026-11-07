"""``hyprial socialware install`` is a loud COMING_SOON placeholder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    return home


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["gui"],
        ["gui", "--yes"],
        ["demo", "--check"],
        ["demo", "--force"],
        ["demo", "--yes", "--check", "--force"],
        ["anything", "extra", "positional"],
        ["gui", "--unknown-future-option"],
    ],
)
def test_install_always_returns_coming_soon_json(monkeypatch, tmp_path: Path, arguments) -> None:
    home = _home(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        _cli_root_app, ["socialware", "install", *arguments, "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": False,
        "code": "COMING_SOON",
        "error": "hyprial socialware install is coming soon",
    }
    # The stub must not touch the application tree it used to manage.
    assert not (home / "apps").exists()


@pytest.mark.parametrize("arguments", [["gui"], ["gui", "--yes"]])
def test_install_human_output_is_a_visible_failure(monkeypatch, tmp_path: Path, arguments) -> None:
    _home(monkeypatch, tmp_path)
    result = CliRunner().invoke(_cli_root_app, ["socialware", "install", *arguments])

    assert result.exit_code == 1
    assert "hyprial socialware install is coming soon" in result.output


def test_install_does_not_require_an_initialized_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "uninitialized"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    result = CliRunner().invoke(
        _cli_root_app, ["socialware", "install", "gui", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "COMING_SOON"


def test_socialware_help_lists_the_reserved_install_command() -> None:
    result = CliRunner().invoke(_cli_root_app, ["--help"])
    assert result.exit_code == 0
    assert "socialware" in result.output
    result = CliRunner().invoke(_cli_root_app, ["socialware", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output
    result = CliRunner().invoke(_cli_root_app, ["socialware", "install", "--help"])
    assert result.exit_code == 0
    assert "retired" in result.output


def test_retired_install_does_not_acquire_a_sidecar(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        _cli_root_app, ["socialware", "install", "demo", "--yes", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "COMING_SOON"
    assert "sidecar" not in result.stdout
    assert not (home / "apps").exists()
    assert not (home / "bin").exists()
