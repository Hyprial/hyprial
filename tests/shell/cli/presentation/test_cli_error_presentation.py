"""Installed-entrypoint presentation for user errors and internal failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer._click.exceptions import MissingParameter

from hyprial.shell.impl.cli.runtime import entry as _cli_entry


ROOT = Path(__file__).resolve().parents[4]
USER_ERRORS = (
    (("message", "send"), "Missing argument"),
    (("definitely-not-a-command",), "No such command"),
)


def _environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["HYPRIAL_HOME"] = str(home)
    environment["HARNESS_STATE_DIR"] = str(state)
    environment["NO_COLOR"] = "1"
    environment.pop("FORCE_COLOR", None)
    return environment


def _run_cli(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", *arguments],
        cwd=ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_catches_the_click_exception_class_used_by_typer() -> None:
    assert issubclass(MissingParameter, _cli_entry.TyperClickException)


@pytest.mark.parametrize(("arguments", "message"), USER_ERRORS)
def test_user_errors_are_one_friendly_line_without_a_traceback(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    result = _run_cli(tmp_path, *arguments)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("hyprial: ")
    assert message in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(("arguments", "message"), USER_ERRORS)
def test_user_errors_keep_the_structured_json_contract(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    result = _run_cli(tmp_path, *arguments, "--json")

    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"
    assert message in payload["error"]


def test_internal_errors_still_escape_with_the_full_traceback(tmp_path: Path) -> None:
    script = """
import sys
from hyprial.shell.impl.cli.runtime import entry as cli

@cli.app.command("internal-error-probe")
def raise_internal_error():
    raise RuntimeError("CLI_ERROR_PRESENTATION_INTERNAL_SENTINEL")

sys.argv = ["hyprial", "internal-error-probe"]
cli.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" in result.stderr
    assert "raise_internal_error" in result.stderr
    assert "RuntimeError: CLI_ERROR_PRESENTATION_INTERNAL_SENTINEL" in result.stderr
