"""Help text for capability grants must match the runtime enforcement boundary."""

from __future__ import annotations

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app


runner = CliRunner()


def _help_text(*args: str) -> str:
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0
    return " ".join(result.output.split())


def test_agent_grant_help_names_the_runtime_enforced_capabilities() -> None:
    output = _help_text("agent", "grant")

    assert "see-actors and send-to" in output
    assert "enforced on each applicable request" in output
    assert "Recording does not enforce runtime permissions" not in output


def test_grant_check_help_says_it_is_what_if_and_does_not_read_the_ledger() -> None:
    output = _help_text("agent", "grant")

    assert "what-if capability decision" in output
    assert "does not read the grant ledger" in output


def test_grant_visibility_help_says_it_is_what_if_and_does_not_read_the_ledger() -> None:
    output = _help_text("agent", "grant")

    assert "what-if see-actors / send-to decisions" in output
    assert "does not read the grant ledger" in output
