from __future__ import annotations

import importlib.util
from pathlib import Path

from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli
from hyprial.daemon.impl.adapters import lark
from hyprial.daemon.impl import application
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app


ROOT = Path(__file__).resolve().parents[3]


def _native_source_tree(root: Path) -> str:
    paths = tuple(sorted(root.rglob("*.py")))
    assert paths, f"native source inventory is empty: {root}"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_removed_packages_and_future_only_exports_cannot_regrow() -> None:
    proto_spec = importlib.util.find_spec("hyprial.proto")
    # A source checkout may retain an untracked ``__pycache__`` directory
    # after the tracked package is deleted.  That is an empty namespace, not
    # a loadable product module.  Gate the actual regression: no loader and
    # no Python source may recreate the retired codec.
    assert proto_spec is None or proto_spec.loader is None
    proto_root = ROOT / "src" / "hyprial" / "proto"
    assert not tuple(proto_root.glob("*.py"))
    assert importlib.util.find_spec("hyprial.daemon.impl.squire.features") is None
    assert not hasattr(lark, "RoomMirrorPort")


def test_retired_irc_channel_cli_and_ipc_are_absent() -> None:
    groups = [group.name for group in _cli_root_app.registered_groups]
    assert "channel" not in groups
    assert groups.count("squire") == 1

    result = CliRunner().invoke(_cli_root_app, ["channel", "list"])
    assert result.exit_code != 0

    daemon_source = _native_source_tree(Path(application.__file__).parent)
    for method in ("channel.list", "channel.join", "channel.part"):
        assert method not in daemon_source


def test_retired_agent_keep_command_errors() -> None:
    result = CliRunner().invoke(_cli_root_app, ["agent", "keep", "add", "worker"])

    assert result.exit_code != 0
    assert "No such command 'keep'" in result.output


def test_retired_r1_command_spellings_cannot_regrow() -> None:
    """R1 moved the agent surface; the old spellings must error, not alias."""

    root = CliRunner()
    for argv in (
        ["start", "claude", "--name", "worker"],
        ["down", "worker"],
        ["down", "--all"],
        ["top"],
        ["transfer", "worker", "--to", "server"],
        ["transfer-runtime", "probe"],
        ["transfer-bundle-validate", "bundle"],
        ["transfer-precheck", "--harness", "codex", "--name", "worker"],
        ["grants-check", "--capability", "tool-surface"],
        ["grants-visibility", "--target", "agent:a:b:c"],
        ["agent", "home-census"],
        ["agent", "migrate", "preflight", "manifest.json"],
        ["agent", "restore-policy", "worker", "always"],
        ["agent", "restore-threshold", "12h"],
        ["agent", "revoke", "worker", "g1"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_org_spellings_cannot_regrow() -> None:
    """R3: `org execute` is deleted and the context commands moved down."""

    root = CliRunner()
    for argv in (
        ["org", "execute", "hyprial-invite:v1:abc"],
        ["org", "show"],
        ["org", "status"],
        ["org", "fetch"],
        ["org", "import", "org-context.md"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_not_implemented_renderer_has_no_producer() -> None:
    source = "\n".join(
        _native_source_tree(Path(cli.__file__).parents[1] / family)
        for family in ("runtime", "commands")
    )
    assert "NOT_IMPLEMENTED" not in source
    assert "NotImplementedError" not in source


def test_retired_workflow_and_routine_spellings_cannot_regrow() -> None:
    root = CliRunner()
    for argv in (
        ["worktrees"],
        ["workflow", "status", "wf-test"],
        ["workflow", "context", "wf-test", "work"],
        ["routine", "rm", "cli-routine"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_probe_spellings_cannot_regrow() -> None:
    root = CliRunner()
    for argv in (
        ["squire", "probe"],
        ["dispatch", "matrix"],
        ["dispatch", "matrix", "--probe"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_host_spellings_cannot_regrow() -> None:
    root = CliRunner()
    for argv in (
        ["hosts"],
        ["network"],
        ["network", "exposures"],
        ["host", "expose", "8443", "--target", "tcp:127.0.0.1:9443"],
        ["host", "unexpose", "8443"],
        ["host", "exposures"],
        ["host", "peer-key", "100.64.20.8:54321"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_top_level_ps_command_cannot_regrow() -> None:
    """R7 deleted top-level ``ps``; ``daemon status``/``agent list`` replace it.

    The daemon IPC method ``ps`` still backs those two commands, so only the
    CLI spelling is asserted absent.
    """

    result = CliRunner().invoke(_cli_root_app, ["ps", "--json"])
    assert result.exit_code != 0
    assert "No such command" in result.output or "Usage" in result.output


def test_e2e_skill_uses_grouped_status_and_refresh_commands() -> None:
    skill = (ROOT / ".agents/skills/hyprial-e2e/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "hyprial ps --json" not in skill
    assert "hyprial ps --refresh" not in skill
    assert "hyprial daemon status --json" in skill
    assert "hyprial host list --refresh --json" in skill


def test_retired_top_level_install_cannot_regrow() -> None:
    result = CliRunner().invoke(_cli_root_app, ["install", "demo"])

    assert result.exit_code != 0
    assert "No such command 'install'" in result.output


def test_retired_r5_command_spellings_cannot_regrow() -> None:
    """R5 moved the daemon/upgrade surface; the old spellings must error, not alias."""

    root = CliRunner()
    for argv in (
        ["restart"],
        ["restart", "--yes"],
        ["upgrade", "--help"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_message_verb_spellings_cannot_regrow() -> None:
    """R12 moved ``send``/``ack``/``reply`` under ``message``; no alias remains.

    The daemon IPC methods (``message.send``/``message.ack``/``message.reply``)
    are unchanged, so only the CLI spelling is asserted absent.
    """

    root = CliRunner()
    for argv in (
        ["send", "--from", "user:op", "--to", "agent:o:m:a", "hi"],
        ["ack", "message-1", "--from", "user:op"],
        ["reply", "message-1", "done", "--from", "user:op"],
    ):
        result = root.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_retired_fs_leaf_spellings_cannot_regrow() -> None:
    """R12 regrouped the fs leaves; the flat spellings must error, not alias.

    ``fs`` now lives under ``org``; its retired flat leaves must still fail.
    The daemon IPC methods (``orgfs.*``) are unchanged.
    """

    root = CliRunner()
    for argv in (
        ["fs", "spaces"],
        ["fs", "create", "team"],
        ["fs", "join", "space-1"],
        ["fs", "status", "space-1"],
        ["fs", "serve", "space-1"],
        ["fs", "checkout", "space-1"],
        ["fs", "members", "space-1"],
        ["fs", "invite", "space-1", "user:reader"],
        ["fs", "remove-member", "space-1", "user:reader"],
        ["fs", "trash", "space-1"],
        ["fs", "restore", "space-1", "doc.md", "v1"],
        ["fs", "purge-plan", "space-1", "[]"],
        ["fs", "purge", "space-1", "plan-1"],
        ["fs", "purge-status", "space-1", "plan-1"],
        ["fs", "unban", "space-1", "sha"],
    ):
        result = root.invoke(_cli_root_app, ["org", *argv])
        assert result.exit_code != 0, argv
