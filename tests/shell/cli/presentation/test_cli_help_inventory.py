"""The custom ``hyprial help`` inventory follows the registered Typer root."""

from __future__ import annotations

import ast
import enum
import json
from pathlib import Path
import re
import shlex

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from hyprial.shell.impl.cli.inventory import (
    registered_command_tree,
    registered_root_parameters,
    render_top_level_help,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.message.observe.batch import message_batch
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


ROOT = Path(__file__).resolve().parents[4]


class _ProbeMode(str, enum.Enum):
    a = "a"
    b = "b"


COMMAND_GUIDANCE = re.compile(r"`hyprial\s+([^`]+)`")
#: The one embedded-JavaScript literal in the product tree.  ``dsh_tui`` writes a
#: JS plugin whose socket logger interpolates; that log is not a command an
#: operator could run.  It is excluded by **exact file and exact span**, never by
#: a pattern: any other ``${...}`` span is still validated, and the entry is
#: asserted below to still be found so it cannot rot into a silent blind spot.
EMBEDDED_JS_GUIDANCE = {
    (
        "src/hyprial/daemon/impl/harnesses/dsh_tui/plugin.py",
        "bridge socket failed: ${String(error)}",
    ): "the written JS plugin's template-literal log, not operator guidance",
}


def _registered_top_level() -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = get_command(_cli_root_app)
    visible: list[str] = []
    hidden: list[str] = []
    for name, command in root.commands.items():
        (hidden if command.hidden else visible).append(name)
    return tuple(visible), tuple(hidden)


def _listed_top_level(usage: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    visible: list[str] = []
    hidden: list[str] = []
    destination: list[str] | None = None
    for line in usage.splitlines():
        if line == "Commands:":
            destination = visible
        elif line == "Hidden commands (internal):":
            destination = hidden
        elif line.startswith("  hyprial ") and destination is not None:
            destination.append(line.removeprefix("  hyprial "))
    return tuple(visible), tuple(hidden)


def _runtime_strings(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, (ast.Constant, ast.JoinedStr))
    }
    joined_values = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
    }
    strings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                value.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                else "<value>"
                for value in node.values
            )
            strings.append((node.lineno, text))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in joined_values
        ):
            strings.append((node.lineno, node.value))
    return tuple(strings)


def _assert_registered_guidance(command_text: str) -> None:
    command = get_command(_cli_root_app)
    for word in shlex.split(command_text):
        commands = getattr(command, "commands", None)
        if word.startswith(("-", "<")) or commands is None:
            break
        assert word in commands, command_text
        command = commands[word]


def _unregistered_guidance(text: str, relative: str) -> list[str]:
    """Backtick command spans in ``text`` that the root does not register.

    ``relative`` is the file the text came from, because the single embedded-JS
    literal is the only thing excluded and it is excluded by its exact span in
    its exact file.
    """

    offenders: list[str] = []
    for match in COMMAND_GUIDANCE.finditer(text):
        command_text = match.group(1).strip()
        if (relative, command_text) in EMBEDDED_JS_GUIDANCE:
            continue
        try:
            _assert_registered_guidance(command_text)
        except AssertionError:
            offenders.append(command_text)
    return offenders


def test_guidance_validator_rejects_unregistered_top_level_command() -> None:
    """The traversal must reach the root's children, or the guard is vacuous."""

    with pytest.raises(AssertionError, match="missing-command"):
        _assert_registered_guidance("missing-command")


def test_scan_rejects_a_retired_command_hidden_behind_js_interpolation() -> None:
    """``${...}`` is not an exclusion; only the exact allowlisted span is."""

    text = 'Full body: `hyprial missing-command ${{ignored}}`.'
    assert _unregistered_guidance(text, "src/hyprial/example.py") == [
        "missing-command ${{ignored}}"
    ]


def test_scan_excludes_only_the_embedded_js_log_in_its_own_file() -> None:
    text = 'ctx.logger?.warn?.(`hyprial bridge socket failed: ${String(error)}`)'
    allowed = "src/hyprial/daemon/impl/harnesses/dsh_tui/plugin.py"

    assert _unregistered_guidance(text, allowed) == []
    # Same span elsewhere is not covered by the exemption.
    assert _unregistered_guidance(text, "src/hyprial/daemon/impl/example.py") == [
        "bridge socket failed: ${String(error)}"
    ]


def test_every_declared_exclusion_is_still_found_in_the_source() -> None:
    """A stale allowlist entry would silently widen the guard; assert it hits."""

    found = set()
    for path in (ROOT / "src/hyprial").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for _line, text in _runtime_strings(path):
            for match in COMMAND_GUIDANCE.finditer(text):
                found.add((relative, match.group(1).strip()))

    assert set(EMBEDDED_JS_GUIDANCE) <= found, (
        "an embedded-JS exclusion no longer matches anything; remove it instead "
        "of leaving a blind spot"
    )


def test_user_facing_command_guidance_names_registered_commands() -> None:
    seen: set[str] = set()
    for path in (ROOT / "src/hyprial").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for line, text in _runtime_strings(path):
            offenders = _unregistered_guidance(text, relative)
            for command_text in offenders:
                raise AssertionError(f"{relative}:{line}: {command_text}")
            for match in COMMAND_GUIDANCE.finditer(text):
                seen.add(match.group(1).strip())

    assert "daemon upgrade" in seen
    assert "doctor --fix" in seen
    # #1854 retired the connector-local summary renderer that supplied the old
    # ``message read <value>`` runtime hint; batch registration is asserted below.


def test_help_inventory_matches_registered_top_level_tree() -> None:
    result = CliRunner().invoke(_cli_root_app, ["help", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    listed = _listed_top_level(payload["usage"])
    assert listed == _registered_top_level()
    assert {
        "workflow",
        "routine",
        "agent",
        "host",
        "daemon",
        "user",
        "org",
        "init",
    } <= set(listed[0])
    assert "restart" not in set(listed[0]) | set(listed[1])
    assert "transfer" not in set(listed[0]) | set(listed[1])
    assert "transfer-runtime" not in set(listed[0]) | set(listed[1])
    assert "start" not in set(listed[0]) | set(listed[1])
    assert "down" not in set(listed[0]) | set(listed[1])
    assert "top" not in set(listed[0]) | set(listed[1])
    assert "grants-check" not in set(listed[0]) | set(listed[1])
    assert "grants-visibility" not in set(listed[0]) | set(listed[1])
    assert "work" not in set(listed[0]) | set(listed[1])
    assert "pac" not in set(listed[0]) | set(listed[1])
    assert "hosts" not in set(listed[0]) | set(listed[1])
    assert "network" not in set(listed[0]) | set(listed[1])
    assert "install" not in set(listed[0]) | set(listed[1])
    assert "mcp" not in set(listed[0]) | set(listed[1])
    assert "upgrade" not in set(listed[0]) | set(listed[1])
    assert "inbox" not in set(listed[0]) | set(listed[1])

    root = get_command(_cli_root_app)
    daemon = root.commands["daemon"]
    assert hasattr(daemon, "commands")
    assert {"status", "start", "run", "stop", "restart", "upgrade", "service"} == set(
        daemon.commands
    )
    service_group = daemon.commands["service"]
    assert hasattr(service_group, "commands")
    assert {"install", "uninstall", "status"} == set(service_group.commands)
    upgrade_group = daemon.commands["upgrade"]
    assert hasattr(upgrade_group, "commands")
    assert {"status", "apply", "run"} == set(upgrade_group.commands)
    assert upgrade_group.commands["run"].hidden is True
    link = root.commands["agent"].commands["link"]
    assert hasattr(link, "commands")
    assert set(link.commands) == {"list", "stats", "add", "drop"}
    routine = root.commands["routine"]
    assert hasattr(routine, "commands")
    assert "set" in routine.commands
    user = root.commands["user"]
    assert hasattr(user, "commands")
    assert {"add", "bind", "bindings", "list", "show", "unbind", "whois", "proxy"} == set(
        user.commands
    )
    bindings = user.commands["bindings"]
    assert hasattr(bindings, "commands")
    assert set(bindings.commands) == {"audit", "publish", "withdraw"}
    org = root.commands["org"]
    assert hasattr(org, "commands")
    context = org.commands["context"]
    assert hasattr(context, "commands")
    assert set(context.commands) == {"show", "status", "fetch", "import", "validate"}
    for retired in ("execute", "show", "status", "fetch", "import"):
        assert retired not in org.commands
    work = org.commands["work"]
    assert hasattr(work, "commands")
    assert set(work.commands) == {
        "add",
        "check",
        "collect",
        "finish",
        "note",
        "status",
        "done",
        "show",
        "ls",
        "link",
        "keywords",
        "repair",
        "resolve",
    }
    workflow = root.commands["workflow"]
    assert hasattr(workflow, "commands")
    assert "missions" not in workflow.commands
    agent = root.commands["agent"]
    assert hasattr(agent, "commands")
    assert {
        "create",
        "host-invite",
        "list",
        "start",
        "stop",
        "top",
        "move",
        "unblock",
        "restore",
        "grant",
        "grants",
        "destroy",
        "secret",
        "home",
        "config",
        "ensure-home",
        "readiness",
        "provider",
        "tiers",
        "transfer",
        "link",
    } == set(agent.commands)
    transfer = agent.commands["transfer"]
    assert transfer.hidden is True
    assert "runtime" in transfer.commands
    home = agent.commands["home"]
    assert set(home.commands) == {"census", "migrate"}
    tiers = agent.commands["tiers"]
    assert hasattr(tiers, "commands")
    assert set(tiers.commands) == {"probe"}
    squire = root.commands["squire"]
    assert hasattr(squire, "commands")
    assert "probe" not in squire.commands
    host = root.commands["host"]
    assert hasattr(host, "commands")
    assert {"create", "list", "status"} <= set(host.commands)
    assert "expose" not in host.commands
    assert "exposures" not in host.commands
    assert "peer-key" not in host.commands
    assert "unexpose" not in host.commands
    service = org.commands["service"]
    assert hasattr(service, "commands")
    assert {
        "connect",
        "disconnect",
        "expose",
        "exposures",
        "guide",
        "list",
        "unexpose",
        "whois",
    } == set(service.commands)
    assert "peer-key" not in service.commands
    assert "ps" not in set(listed[0]) | set(listed[1])
    assert "send" not in set(listed[0]) | set(listed[1])
    assert "ack" not in set(listed[0]) | set(listed[1])
    assert "reply" not in set(listed[0]) | set(listed[1])
    message = root.commands["message"]
    assert hasattr(message, "commands")
    assert {"send", "ack", "reply", "batch"} <= set(message.commands)
    fs = org.commands["fs"]
    assert hasattr(fs, "commands")
    assert {"space", "member", "trash", "purge"} <= set(fs.commands)
    for retired_fs in (
        "spaces",
        "members",
        "invite",
        "remove-member",
        "restore",
        "purge-plan",
        "purge-status",
        "unban",
        "create",
        "join",
        "status",
        "serve",
        "web",
        "checkout",
    ):
        assert retired_fs not in fs.commands, retired_fs
    assert set(fs.commands["space"].commands) == {
        "list",
        "create",
        "join",
        "status",
        "serve",
        "web",
        "checkout",
    }
    assert set(fs.commands["member"].commands) == {"list", "invite", "remove"}
    assert set(fs.commands["trash"].commands) == {"list", "restore"}
    assert set(fs.commands["purge"].commands) == {"plan", "run", "status", "unban"}
    socialware = root.commands["socialware"]
    assert hasattr(socialware, "commands")
    assert set(socialware.commands) == {"install"}


def test_renderer_includes_new_and_hidden_registrations_without_a_snapshot() -> None:
    sample = typer.Typer()

    @sample.command("visible")
    def visible() -> None:
        """Visible probe."""

    @sample.command("internal", hidden=True)
    def internal() -> None:
        """Hidden probe."""

    usage = render_top_level_help(sample)

    assert _listed_top_level(usage) == (("visible",), ("internal",))


def test_registered_command_tree_is_recursive_and_marks_real_executables() -> None:
    """The generated snapshot must include nested groups, leaves and hidden subtrees."""

    entries = {entry.path: entry for entry in registered_command_tree(_cli_root_app)}

    assert "agent" in entries
    assert entries["agent"].kind == "group"
    assert entries["agent"].executable is False
    assert entries["agent move"].kind == "leaf"
    # A group callback is an operator entry point; a plain group only dispatches.
    assert entries["agent tiers"].executable is True
    assert entries["agent transfer"].hidden is True
    assert entries["agent transfer runtime probe"].hidden is True
    assert entries["daemon upgrade run"].hidden is True


def test_registered_command_tree_uses_real_group_and_hidden_semantics() -> None:
    sample = typer.Typer()
    empty = typer.Typer()
    sample.add_typer(empty, name="empty")
    executable = typer.Typer(invoke_without_command=True)

    @executable.callback()
    def executable_callback() -> None:
        """Run without a subcommand."""

    @executable.command("sub")
    def sub() -> None:
        """A child."""

    sample.add_typer(executable, name="executable")
    hidden = typer.Typer(hidden=True)

    @hidden.command("child")
    def hidden_child() -> None:
        """A hidden child."""

    sample.add_typer(hidden, name="hidden")

    entries = {entry.path: entry for entry in registered_command_tree(sample)}

    # An empty Typer group is still a group, not a leaf that cannot execute.
    assert entries["empty"].kind == "group"
    assert entries["empty"].executable is False
    assert entries["executable"].kind == "group"
    assert entries["executable"].executable is True
    assert entries["hidden"].direct_hidden is True
    assert entries["hidden"].hidden is True
    assert entries["hidden child"].direct_hidden is False
    assert entries["hidden child"].hidden is True


def test_registered_parameters_capture_operator_constraints_and_root_params() -> None:
    sample = typer.Typer()

    @sample.callback()
    def root(
        root_flag: bool = typer.Option(False, "--root-flag", help="root help"),
    ) -> None:
        """Root callback."""

    @sample.command("bounded")
    def bounded(
        value: int = typer.Option(1, "--value", min=1, max=5, help="bounded help"),
        mode: _ProbeMode = typer.Option(_ProbeMode.a, "--mode", help="mode help"),
        tags: list[str] = typer.Argument(None, help="tags help"),
    ) -> None:
        """Bounded parameters."""

    root_options = {option for param in registered_root_parameters(sample) for option in param.opts}
    assert "--root-flag" in root_options

    entries = {entry.path: entry for entry in registered_command_tree(sample)}
    params = {param.opts[0]: param for param in entries["bounded"].params}

    assert params["--value"].type == "int range"
    assert (params["--value"].minimum, params["--value"].maximum) == (1, 5)
    assert params["--value"].default == 1
    assert params["--value"].help == "bounded help"
    assert params["--mode"].type == "choice"
    assert params["--mode"].choices == ("a", "b")
    assert params["--mode"].default is _ProbeMode.a
    assert params["tags"].nargs == -1
    assert params["tags"].help == "tags help"


def test_registered_command_tree_records_parameters_in_declaration_order() -> None:
    entries = {entry.path: entry for entry in registered_command_tree(_cli_root_app)}

    inspect_options = [
        option for param in entries["workflow inspect"].params for option in param.opts
    ]
    assert "--node" in inspect_options
    batch_options = [
        option for param in entries["message batch"].params for option in param.opts
    ]
    assert batch_options.index("--cursor") < batch_options.index("--after-cursor")


def test_mutually_exclusive_batch_cursors_fail_before_any_daemon_request() -> None:
    """A parameter-exclusivity regression must fail, not silently pick one cursor."""

    with pytest.raises(typer.BadParameter, match="mutually exclusive"):
        message_batch(source="probe", cursor="before", after_cursor="after", json_output=True)


def test_upgrade_help_hides_one_shot_source_options() -> None:
    """R5 hides the internal one-shot source flags from help; they stay usable."""

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--help"])

    assert result.exit_code == 0
    assert "--source" not in result.output
    assert "--ref" not in result.output
    # Hiding is a help change, not a deletion: the option objects still parse.
    upgrade_group = get_command(_cli_root_app).commands["daemon"].commands["upgrade"]
    option_names = {
        option
        for param in upgrade_group.params
        for option in getattr(param, "opts", ())
    }
    assert {"--source", "--ref"} <= option_names
