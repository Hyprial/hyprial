"""The complete Typer/Click command surface is an explicitly reviewed contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from hyprial.shell.impl.cli.runtime import entry as cli
from hyprial.shell.impl.cli.surface import (
    REGENERATE_COMMAND,
    extract_cli_surface,
    format_cli_surface_diff,
)

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT = ROOT / "contract/cli/surface.json"


def test_cli_surface_matches_committed_snapshot() -> None:
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = extract_cli_surface(cli.app)

    assert actual == expected, format_cli_surface_diff(expected, actual)


def test_surface_diff_groups_commands_and_options() -> None:
    actual = extract_cli_surface(cli.app)
    expected = copy.deepcopy(actual)
    expected["commands"].append(
        {
            "arguments": [],
            "deprecated": False,
            "help": "Synthetic diff assertion.",
            "hidden": False,
            "name": "synthetic-removed",
            "options": [],
            "path": "hyprial synthetic-removed",
        }
    )
    expected["commands"][0]["options"].append(
        {
            "default": False,
            "flags": ["--retired-test-option"],
            "is_flag": True,
            "multiple": False,
            "name": "retired_test_option",
            "required": False,
        }
    )
    actual["commands"].append(
        {
            "arguments": [],
            "deprecated": False,
            "help": "Synthetic diff assertion.",
            "hidden": False,
            "name": "synthetic-added",
            "options": [],
            "path": "hyprial synthetic-added",
        }
    )

    diff = format_cli_surface_diff(expected, actual)

    assert "- hyprial synthetic-removed" in diff
    assert "+ hyprial synthetic-added" in diff
    assert "- option --retired-test-option" in diff
    assert f"Regenerate with: {REGENERATE_COMMAND}" in diff
