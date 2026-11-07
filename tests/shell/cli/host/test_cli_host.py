from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.mark.parametrize("command", ["status", "create"])
def test_reserved_host_commands_fail_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))

    result = CliRunner().invoke(_cli_root_app, ["host", command, "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "code": "COMING_SOON",
        "error": f"hyprial host {command} is coming soon",
    }
    assert not home.exists()


def test_host_list_human_heading_uses_canonical_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"hosts": [{"nodeId": "node-a", "status": "online"}]},
    )

    result = CliRunner().invoke(_cli_root_app, ["host", "list"])

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == "hyprial host list   1 nodes"
