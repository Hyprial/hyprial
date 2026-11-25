from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
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
    assert importlib.util.find_spec("hyprial.daemon.impl.squire") is None
    assert not hasattr(lark, "RoomMirrorPort")


def test_retired_irc_channel_cli_and_ipc_are_absent() -> None:
    groups = [group.name for group in _cli_root_app.registered_groups]
    assert "channel" not in groups
    assert "squire" not in groups

    result = CliRunner().invoke(_cli_root_app, ["channel", "list"])
    assert result.exit_code != 0

    daemon_source = _native_source_tree(Path(application.__file__).parent)
    for method in ("channel.list", "channel.join", "channel.part"):
        assert method not in daemon_source


def test_retired_pin_and_authority_cli_and_ipc_are_absent() -> None:
    """I6-4: adapter pins and the public authority.* surface are retired."""

    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    for method in (
        "adapter.pin",
        "adapter.pins",
        "adapter.unpin",
        "authority.get",
        "authority.list",
        "authority.set",
    ):
        assert METHOD_REGISTRY.get(method) is None

    from typer.main import get_command

    root = get_command(_cli_root_app)
    agent_group = root.commands["agent"]
    assert "authority" not in agent_group.commands
    assert "cell" in agent_group.commands
    adapter_group = root.commands["adapter"]
    assert {"pin", "unpin"}.isdisjoint(adapter_group.commands)

    runner = CliRunner()
    for argv in (
        ["adapter", "pin", "lark-main", "worker"],
        ["adapter", "unpin", "lark-main"],
        ["agent", "authority", "set", "worker"],
        ["agent", "authority", "get", "worker"],
        ["agent", "authority", "list"],
    ):
        result = runner.invoke(_cli_root_app, argv)
        assert result.exit_code != 0, argv


def test_shipped_text_never_teaches_the_retired_pin_commands() -> None:
    """I6-4: shipped skills, routine templates and CLI notes teach ``agent cell``."""

    import re

    retired = re.compile(r"hyprial adapter (?:un)?pins?\b|hyprial agent authority\b|adapter pin\b")
    shipped = (
        ROOT / "src" / "hyprial" / "skills" / "hyprial-ops" / "SKILL.md",
        ROOT / "src" / "hyprial" / "biz" / "impl" / "routine" / "templates" / "agent-home-setup.yaml",
        ROOT / "src" / "hyprial" / "daemon" / "impl" / "bootstrap" / "adapter_registration.py",
        ROOT / "src" / "hyprial" / "shell" / "impl" / "cli" / "commands" / "adapter" / "admin.py",
    )
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in shipped
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if retired.search(line)
    ]
    assert offenders == []


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


def test_retired_agent_link_and_message_thread_cli_cannot_regrow() -> None:
    """T2a removed ``hyprial agent link`` and ``hyprial message thread`` without aliases."""

    runner = CliRunner()
    for argv in (
        ["agent", "link"],
        ["agent", "link", "list", "--from", "agent:op:node:alice"],
        ["agent", "link", "stats", "--from", "agent:op:node:alice"],
        ["agent", "link", "add", "agent:op:node:bob", "--from", "agent:op:node:alice"],
        ["agent", "link", "drop", "agent:op:node:bob", "--from", "agent:op:node:alice"],
        ["link"],
        ["link", "list", "--json"],
        ["message", "thread"],
        ["message", "thread", "list"],
        ["message", "thread", "show", "t." + "a" * 26],
    ):
        result = runner.invoke(_cli_root_app, argv)
        assert result.exit_code == 2, (argv, result.exit_code, result.output)
        assert "moved:" not in (result.stdout or "") + (result.stderr or ""), argv

    link_spec = importlib.util.find_spec("hyprial.shell.impl.cli.commands.link")
    assert link_spec is None or link_spec.loader is None
    assert not tuple((ROOT / "src" / "hyprial" / "shell" / "impl" / "cli" / "commands" / "link").glob("*.py"))
    assert importlib.util.find_spec("hyprial.shell.impl.cli.commands.message.threads.thread") is None


def test_retired_link_and_thread_requests_and_contracts_cannot_regrow() -> None:
    """T2b retired ``link.*``, ``threads.*``, link error codes, hooks and facade re-exports."""

    import hyprial.daemon as daemon_face
    from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
    from hyprial.daemon.impl.request_catalog.daemon.readiness import READINESS_PROBES
    from hyprial.daemon.impl.request_catalog.migration import MIGRATED_FAMILIES, MIGRATED_METHODS
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
    from hyprial.error import codes
    from hyprial.kernel import EVENT_NAMES, ipc_errors

    retired_methods = {"link.list", "link.stats", "link.add", "link.drop", "threads.list", "threads.get"}
    assert retired_methods.isdisjoint(set(METHOD_REGISTRY.names))
    assert retired_methods.isdisjoint(MIGRATED_METHODS)
    assert retired_methods.isdisjoint(_IPC_STATS_METHODS)
    assert "link" not in MIGRATED_FAMILIES
    assert "link-service" not in READINESS_PROBES

    for module_name in (
        "hyprial.daemon.impl.request_catalog.link",
        "hyprial.daemon.impl.request_catalog.link_examples",
        "hyprial.daemon.impl.request_catalog.threads",
    ):
        spec = importlib.util.find_spec(module_name)
        assert spec is None or spec.loader is None, module_name

    for code in (
        "LINK_MINTING",
        "LINK_PEER_FULL",
        "LINK_RECLAIMED",
        "LINK_SELF_FULL",
        "LINK_UNKNOWN",
        "LINK_WINDOW_FULL",
    ):
        assert not hasattr(codes, code), code
        assert not hasattr(ipc_errors, code), code
        assert code not in ipc_errors.ALL_CODES, code

    assert "link.minted" not in EVENT_NAMES
    assert "link.reclaimed" not in EVENT_NAMES
    from hyprial.kernel.impl.hooks.config import HooksConfig

    for event in ("link.minted", "link.reclaimed"):
        with pytest.raises(ValueError, match="unknown hook event"):
            HooksConfig.from_json(
                {
                    "schemaVersion": 2,
                    "hooks": [
                        {
                            "name": "retired-hook",
                            "events": [event],
                            "kind": "observe",
                            "command": ["true"],
                        }
                    ]
                }
            )

    for symbol in (
        "HOLD_REASONS",
        "HOLD_REASON_KEY",
        "IPC_LINK_ADD",
        "IPC_LINK_DROP",
        "IPC_LINK_LIST",
        "IPC_LINK_STATS",
        "LinkAdmin",
        "LinkChangeSource",
        "LinkGate",
        "LinkQueries",
        "LinkSendObserver",
        "TOP_FIELD_CAPACITY_W",
        "TOP_FIELD_INBOUND_AWAITING_INJECTION",
        "TOP_FIELD_OUTBOUND_IN_FLIGHT",
    ):
        assert not hasattr(daemon_face, symbol), symbol
        assert symbol not in daemon_face.__all__, symbol

    assert not (ROOT / "contract" / "wire" / "send-links.md").exists()
    assert not (ROOT / "contract" / "wire" / "send-links.json").exists()

    conv_list = METHOD_REGISTRY.get("conversations.list")
    assert conv_list is not None
    assert "threadId" not in conv_list.spec.params_schema["properties"]
    conv_hist = METHOD_REGISTRY.get("conversations.history")
    assert conv_hist is not None
    assert "threadId" not in conv_hist.spec.result_schema["properties"]
    send_spec = METHOD_REGISTRY.get("message.send")
    assert send_spec is not None
    assert "holdReason" not in send_spec.spec.result_schema["properties"]
    delivery_props = send_spec.spec.result_schema["properties"]["deliveries"]["items"]["properties"]
    assert {"holdReason", "detail", "evicted"}.isdisjoint(delivery_props)
    outbox_spec = METHOD_REGISTRY.get("outbox.list")
    assert outbox_spec is not None
    assert "holdReason" not in outbox_spec.spec.result_schema["properties"]["entries"]["items"]["properties"]
    query_spec = METHOD_REGISTRY.get("message.query")
    assert query_spec is not None
    assert "holdReason" not in query_spec.spec.result_schema["properties"]["entries"]["items"]["properties"]


def test_retired_link_and_thread_packages_cannot_regrow() -> None:
    """T1c removed ``daemon.impl.links``, ``daemon.impl.threads``, and their composition."""

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.shutdown import _CLOSE_UNBUDGETED_STEPS
    from hyprial.daemon.impl.application.wiring import services as wiring_services
    from hyprial.daemon.impl.cells.params import PARAM_REGISTRY

    for module_name in (
        "hyprial.daemon.impl.links",
        "hyprial.daemon.impl.links.ports",
        "hyprial.daemon.impl.links.service",
        "hyprial.daemon.impl.threads",
        "hyprial.daemon.impl.threads.ids",
        "hyprial.daemon.impl.threads.service",
        "hyprial.daemon.impl.composition.links",
        "hyprial.daemon.impl.composition.link_spaces",
        "hyprial.daemon.impl.composition.threads",
    ):
        try:
            spec = importlib.util.find_spec(module_name)
        except ModuleNotFoundError:
            spec = None
        assert spec is None or spec.loader is None, module_name

    for rel_dir in (
        "daemon/impl/links",
        "daemon/impl/threads",
        "daemon/impl/composition/link_spaces",
        "daemon/impl/composition/threads",
    ):
        assert not (ROOT / "src" / "hyprial" / rel_dir).exists(), rel_dir
    assert not (ROOT / "src" / "hyprial" / "daemon" / "impl" / "composition" / "links.py").exists()

    for rel_test_dir in (
        "tests/daemon/links",
        "tests/daemon/threads",
        "tests/support/links",
    ):
        assert not (ROOT / rel_test_dir).exists(), rel_test_dir

    import inspect

    startup_src = inspect.getsource(wiring_services._WiringServicesMixin._startup_adopt_domain_services)
    assert "compose_link_service" not in startup_src
    assert "link-service" not in dict(_CLOSE_UNBUDGETED_STEPS)
    assert "threads.policy" not in PARAM_REGISTRY
    app_init_src = inspect.getsource(DaemonApplication.__init__)
    assert "_thread_profiles" not in app_init_src
    assert "_owner_binding_profiles" not in app_init_src


def test_retired_link_ledger_and_admission_cannot_regrow() -> None:
    """T1d removed ``inbox/service/link_ledger.py``, ``LinkAdmission``, and ``outbox.link_hold``."""

    from dataclasses import fields

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.dispatch import admission as dispatch_admission
    from hyprial.daemon.impl.inbox.contracts.api import OutboxItem

    try:
        spec = importlib.util.find_spec("hyprial.daemon.impl.inbox.service.link_ledger")
    except ModuleNotFoundError:
        spec = None
    assert spec is None or spec.loader is None
    assert not (
        ROOT / "src" / "hyprial" / "daemon" / "impl" / "inbox" / "service" / "link_ledger.py"
    ).exists()

    for attr in ("LinkAdmission", "LinkGate", "link_governed", "LINK_HELD_EVENT", "LINK_ADMITTED_EVENT"):
        assert not hasattr(dispatch_admission, attr), attr

    assert not hasattr(DaemonApplication, "link_admission")
    assert "link_hold" not in {f.name for f in fields(OutboxItem)}


def test_retired_user_proxy_cli_cannot_regrow() -> None:
    """I7b-1 deleted ``hyprial user proxy setup|status`` and ``agent start user-proxy``."""

    from typer.main import get_command

    from hyprial.shell.impl.cli.runtime.legacy_paths import MOVED_PREFIXES

    root = get_command(_cli_root_app)
    assert "proxy" not in root.commands["user"].commands
    assert "user-proxy" not in root.commands
    assert (("user-proxy",), ("user", "proxy")) not in MOVED_PREFIXES
    assert importlib.util.find_spec("hyprial.shell.impl.cli.commands.squire") is None

    runner = CliRunner()
    for argv in (["user", "proxy", "setup"], ["user", "proxy", "status"]):
        result = runner.invoke(_cli_root_app, argv)
        assert result.exit_code == 2, (argv, result.output)
    started = runner.invoke(
        _cli_root_app,
        ["agent", "start", "user-proxy", "--name", "relay", "--json", "--", "--route", "route:lark:dm"],
    )
    assert started.exit_code == 1, started.output
    assert "harness kind must be claude, pi, codex, jev, or agy" in started.output


def test_retired_authority_set_packages_cannot_regrow() -> None:
    """I6-5b deleted the standalone AuthoritySet store, supervisor, audit and service.

    Member allow/deny is cell-document data applied by member reconcile; apply
    outcomes are ``cell.member_policy_*`` events, not a JSONL audit file.
    """

    import inspect

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.shutdown import _CLOSE_UNBUDGETED_STEPS
    from hyprial.daemon.impl.application.wiring import services as wiring_services

    for module_name in (
        "hyprial.daemon.impl.authority_set",
        "hyprial.daemon.impl.authority_set.audit",
        "hyprial.daemon.impl.authority_set.store",
        "hyprial.daemon.impl.authority_set.supervisor",
        "hyprial.daemon.impl.authority_service",
        "hyprial.daemon.impl.authority_service.service",
    ):
        try:
            spec = importlib.util.find_spec(module_name)
        except ModuleNotFoundError:
            spec = None
        assert spec is None or spec.loader is None, module_name

    for rel_dir in ("daemon/impl/authority_set", "daemon/impl/authority_service"):
        assert not (ROOT / "src" / "hyprial" / rel_dir).exists(), rel_dir
    for rel_test_dir in ("tests/daemon/authority_set", "tests/daemon/authority_service"):
        assert not (ROOT / rel_test_dir).exists(), rel_test_dir

    services_src = inspect.getsource(wiring_services)
    assert "start_authority_service" not in services_src
    assert "authority-supervisor" not in dict(_CLOSE_UNBUDGETED_STEPS)
    app_init_src = inspect.getsource(DaemonApplication.__init__)
    assert "_authority_service" not in app_init_src
    assert "_cell_member_policies" in app_init_src


def test_retired_route_config_twin_cannot_regrow() -> None:
    """F1a: ``adapter.route-config.*`` is retired; ``adapter.routes.*`` is the only route surface."""

    import json

    from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS, ROUTER_ONLY_METHODS
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    retired = {f"adapter.route-config.{action}" for action in ("list", "candidates", "add", "remove")}
    assert retired.isdisjoint(set(METHOD_REGISTRY.names))
    assert retired.isdisjoint(MIGRATED_METHODS | ROUTER_ONLY_METHODS)
    assert {f"adapter.routes.{action}" for action in ("list", "candidates", "add", "remove")} <= set(METHOD_REGISTRY.names)
    for module_name in (
        "hyprial.daemon.impl.request_catalog.adapter.routes.compat",
        "hyprial.daemon.impl.request_catalog.adapter.routes.compat_examples",
    ):
        spec = importlib.util.find_spec(module_name)
        assert spec is None or spec.loader is None, module_name
    openapi = (ROOT / "contract" / "daemon-request" / "openapi.json").read_text(encoding="utf-8")
    assert "adapter.route-config." not in openapi
    surface = json.loads((ROOT / "contract" / "cli" / "surface.json").read_text(encoding="utf-8"))
    rows = [row for row in surface["commands"] if row.get("path") == "hyprial adapter route add"]
    assert len(rows) == 1
    flags = {flag for option in rows[0]["options"] for flag in option["flags"]}
    assert "--default" in flags
    assert "--force" not in flags
    cli_source = (ROOT / "src" / "hyprial" / "shell" / "impl" / "cli" / "commands" / "adapter" / "routes.py").read_text(encoding="utf-8")
    assert "\"adapter.route-config." not in cli_source
