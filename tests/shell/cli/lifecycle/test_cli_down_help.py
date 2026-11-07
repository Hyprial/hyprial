"""Tests for ``hyprial agent stop --help`` accurately describing ``--all``'s blast radius.

Defect: the old help text ("Stop one or all harness connectors") let a reader
believe ``--all`` stops every harness-bridge connector, including interactive
Claude sessions. In practice ``--all`` only stops and deregisters
daemon-managed harness connectors (``hyprial agent start --headless``);
interactive sessions registered via ``hyprial agent start claude --name ...``
(no ``--headless``) are tracked separately in desired state and are never
touched. Adapters are not agents at all and are managed by ``adapter stop``.
These tests pin the corrected wording so the help text cannot silently regress
back to the misleading claim.
"""

from __future__ import annotations

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app

runner = CliRunner()


def test_stop_help_warns_all_does_not_stop_interactive_sessions() -> None:
    result = runner.invoke(app, ["agent", "stop", "--help"])
    assert result.exit_code == 0
    output = " ".join(result.output.split())
    assert "interactive claude sessions" in output.lower()
    assert "does not touch" in output.lower() or "does not reach" in output.lower()


def test_stop_help_documents_permanent_deregistration() -> None:
    result = runner.invoke(app, ["agent", "stop", "--help"])
    assert result.exit_code == 0
    output = result.output.lower()
    # The old wording ("stop") implied a pause; --all also deregisters from
    # desired state, so a stopped connector will not come back on daemon
    # restart until explicitly started again.
    assert "deregister" in output
    assert "restart" in output


def test_stop_help_says_adapters_are_not_agents() -> None:
    result = runner.invoke(app, ["agent", "stop", "--help"])
    assert result.exit_code == 0
    output = result.output.lower()
    assert "adapter" in output
    assert "adapter stop" in output


def test_stop_help_no_longer_uses_the_bare_misleading_summary() -> None:
    result = runner.invoke(app, ["agent", "stop", "--help"])
    assert result.exit_code == 0
    # The historical one-liner made no distinction between connectors,
    # adapters, and interactive sessions; guard against reverting to it.
    assert "Stop one or all harness connectors" not in result.output
