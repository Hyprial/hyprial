"""E2E-010 selects the installed old CLI surface, not its fixture version stamp."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from contract.e2e._upgrade_continuity import installation


@pytest.mark.parametrize(
    ("grouped_exit", "legacy_exit", "expected"),
    [
        (0, 2, ["daemon", "upgrade", "--json"]),
        (2, 0, ["upgrade", "--json"]),
    ],
    ids=["v055-grouped", "v051-legacy"],
)
def test_upgrade_command_uses_installed_cli_help(
    grouped_exit: int, legacy_exit: int, expected: list[str]
) -> None:
    calls: list[list[str]] = []
    environment = {"HOME": "/tmp/hr-upgrade-command-test"}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"] is environment
        assert kwargs["timeout"] == 30
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        code = grouped_exit if argv[1:3] == ["daemon", "upgrade"] else legacy_exit
        assert argv[-1] == "--help", "surface probes must not perform an upgrade"
        return subprocess.CompletedProcess(argv, code, "help", "")

    selected = installation.upgrade_command(
        {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
        Path("/tmp/hr-upgrade-command-test/bin/hyprial"),
        environment,
    )
    assert selected == expected
    assert len(calls) == (1 if grouped_exit == 0 else 2)


def test_upgrade_command_refuses_when_neither_surface_exists() -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "No such command")

    with pytest.raises(AssertionError, match="neither daemon upgrade nor upgrade"):
        installation.upgrade_command(
            {"subprocess": type("Processes", (), {"run": staticmethod(run)})},
            Path("/tmp/hr-upgrade-command-test/bin/hyprial"),
            {"HOME": "/tmp/hr-upgrade-command-test"},
        )
