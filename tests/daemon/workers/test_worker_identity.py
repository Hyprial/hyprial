"""Daemon-managed headless workers speak to the daemon as their OWN actor.

These tests fence the "worker session identity collision": a daemon-launched
headless worker must not inherit the coordinator's ambient harness-bridge MCP
config.  They verify (1) worker launch injects a harness-bridge server carrying
the worker's canonical actor, (2) the worker's setting sources exclude the
ambient server -- proven end-to-end through the real isolated ``_options()``
parser -- and (3) the daemon accepts the worker's session so ``identity.whoami``
returns the WORKER actor and its fenced calls pass.

None of this can be verified live without a coordinator restart; see the PR.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.mcp import entry as mcp_entry
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import agent_sdk as agent_sdk_module
from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient, create_claude_sdk_client
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher
from hyprial.daemon.impl.harnesses.worker_channel import (
    WORKER_CLI_ALLOW_TOOLS,
    WORKER_CLI_DENY_TOOLS,
    WORKER_HARNESS_TOOLS,
    WorkerChannel,
    build_worker_channel,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

CANONICAL = "agent:allen:node-1:kanban-watcher"


def _worker_channel() -> WorkerChannel:
    return build_worker_channel(
        actor=CANONICAL,
        session_ref="sess-xyz",
        hyprial_home=Path("/h/home"),
        state_dir=Path("/h/state"),
    )


def _arg(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# --- Requirement 1: worker launch carries the worker's canonical actor -------


def test_build_worker_channel_pins_canonical_actor_and_this_daemon() -> None:
    channel = _worker_channel()
    server = channel.mcp_server

    assert server["type"] == "stdio"
    args = server["args"]
    assert args[:3] == ["-m", "hyprial.daemon.impl.mcp.entry", "agent-channel"]
    assert _arg(args, "--actor") == CANONICAL
    assert "--session-ref" not in args
    # The stdio child is pinned to THIS daemon, not the worker's inherited env.
    assert server["env"] == {
        "HYPRIAL_HOME": "/h/home",
        "HARNESS_STATE_DIR": "/h/state",
        "HYPRIAL_WORKER_SESSION_REF": "sess-xyz",
    }
    assert "mcp__harness-bridge__harness_send" in channel.allowed_tools


def test_isolated_client_injects_worker_server_and_excludes_ambient() -> None:
    spec = HarnessLaunchSpec(
        harness="claude", name="kanban-watcher", headless=True, cwd="/work"
    )
    client = create_claude_sdk_client(spec, worker_channel=_worker_channel())

    assert isinstance(client, IsolatedAgentSdkClient)
    options = client.options
    # Requirement 2: setting sources exclude the ambient harness-bridge server.
    # "project" is kept on purpose (CLAUDE.md + project permissions); user/local
    # are dropped and strict MCP config excludes settings-file servers entirely.
    assert options["settingSources"] == ["project"]
    assert "user" not in options["settingSources"]
    assert options["strictMcpConfig"] is True
    server = options["mcpServers"]["harness-bridge"]
    assert _arg(server["args"], "--actor") == CANONICAL
    assert "mcp__harness-bridge__harness_send" in options["allowedTools"]
    assert "Bash(hyprial:*)" in options["allowedTools"]
    assert options["disallowedTools"] == list(WORKER_CLI_DENY_TOOLS)


def test_worker_cli_policy_allows_the_product_cli_but_denies_state_changes() -> None:
    """Allow ``hyprial`` but never daemon/org/init or destructive operations.

    The allow half is useless for a headless worker without the deny half:
    ``Bash(hyprial:*)`` also matches ``hyprial daemon restart``.
    """

    channel = _worker_channel()

    assert channel.allowed_tools[-len(WORKER_CLI_ALLOW_TOOLS) :] == WORKER_CLI_ALLOW_TOOLS
    assert channel.disallowed_tools == WORKER_CLI_DENY_TOOLS
    assert set(channel.disallowed_tools) == {
        "Bash(hyprial agent destroy:*)",
        "Bash(hyprial agent stop:*)",
        "Bash(hyprial daemon:*)",
        "Bash(hyprial init:*)",
        "Bash(hyprial org:*)",
        "Bash(hyprial daemon upgrade:*)",
    }


def test_worker_cli_policy_denies_deploy_and_destructive_commands() -> None:
    """Agent stop/upgrade belong to the infra window; destroying an agent is
    destructive. Both halves of the policy must reach the worker."""

    channel = _worker_channel()
    deny = (
        "Bash(hyprial agent stop:*)",
        "Bash(hyprial daemon upgrade:*)",
        "Bash(hyprial agent destroy:*)",
    )
    for rule in deny:
        assert rule in channel.disallowed_tools
    assert list(WORKER_CLI_DENY_TOOLS) == sorted(WORKER_CLI_DENY_TOOLS)

    spec = HarnessLaunchSpec(
        harness="claude", name="kanban-watcher", headless=True, cwd="/work"
    )
    client = create_claude_sdk_client(spec, worker_channel=channel)
    for rule in deny:
        assert rule in client.options["disallowedTools"]


def test_no_worker_channel_leaves_options_untouched() -> None:
    """The pre-fix path is byte-identical: no channel means no MCP override."""

    spec = HarnessLaunchSpec(
        harness="claude", name="kanban-watcher", headless=True, cwd="/work"
    )
    options = create_claude_sdk_client(spec).options

    assert options == {"cwd": "/work", "model": None, "extraArgs": {}}
    assert "settingSources" not in options
    assert "mcpServers" not in options


def test_launcher_only_mints_a_channel_when_a_factory_is_wired() -> None:
    spec = HarnessLaunchSpec(harness="claude", name="kanban-watcher", headless=True)

    assert HarnessLauncher()._worker_channel_for(spec) is None

    minted: list[str] = []

    def factory(spec: HarnessLaunchSpec) -> WorkerChannel:
        minted.append(spec.name)
        return _worker_channel()

    channel = HarnessLauncher(worker_channel_factory=factory)._worker_channel_for(spec)
    assert channel is not None and channel.actor == CANONICAL
    assert minted == ["kanban-watcher"]


# --- Requirement 2 (end of chain): the real _options() applies the fields ----


def test_isolated_worker_options_reach_claude_agent_options(tmp_path: Path) -> None:
    """Run the real isolated ``_options()`` against a recording SDK stand-in."""

    fake_package = tmp_path / "claude_agent_sdk"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        """
import json, os

class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        dump = os.environ.get("HYPRIAL_TEST_OPTIONS_DUMP")
        if dump:
            with open(dump, "w", encoding="utf-8") as stream:
                json.dump(kwargs, stream)

class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

class ResultMessage:
    def __init__(self, result, is_error=False):
        self.result = result
        self.is_error = is_error
""",
        encoding="utf-8",
    )
    worker = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
    spec = HarnessLaunchSpec(
        harness="claude", name="kanban-watcher", headless=True, cwd="/work"
    )
    # Real serialization of the options the daemon would hand a worker.
    client = IsolatedAgentSdkClient(
        spec, worker_channel=_worker_channel(), worker_command=(sys.executable,)
    )
    options_json = client._env["HYPRIAL_AGENT_SDK_OPTIONS"]
    dump_path = tmp_path / "options.json"

    result = subprocess.run(
        [sys.executable, str(worker), "--probe"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(tmp_path),
            "HYPRIAL_AGENT_SDK_OPTIONS": options_json,
            "HYPRIAL_TEST_OPTIONS_DUMP": str(dump_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    dumped = json.loads(dump_path.read_text(encoding="utf-8"))
    assert dumped["setting_sources"] == ["project"]
    assert dumped["strict_mcp_config"] is True
    server = dumped["mcp_servers"]["harness-bridge"]
    assert _arg(server["args"], "--actor") == CANONICAL
    assert "mcp__harness-bridge__harness_send" in dumped["allowed_tools"]
    assert "Bash(hyprial:*)" in dumped["allowed_tools"]
    assert dumped["disallowed_tools"] == list(WORKER_CLI_DENY_TOOLS)
    # The deploy/destructive rules must survive the daemon -> worker hand-off,
    # not only exist in the constant.
    for rule in (
        "Bash(hyprial agent stop:*)",
        "Bash(hyprial daemon upgrade:*)",
        "Bash(hyprial agent destroy:*)",
    ):
        assert rule in dumped["disallowed_tools"]


# --- Requirement 3: the daemon accepts the worker session --------------------


class _FakeInbox:
    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        return ()


class _FakePresence:
    def online_actors(self):  # type: ignore[no-untyped-def]
        return ()


class _FakeHarnesses:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str | None] = {}

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str | None]:
        return dict(self.refs)

    def status(self) -> tuple[()]:
        return ()


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.pop()._agent_session_domains.close()


def _daemon(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="allen",
    **_deep_module_daemon_dependencies())
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    _TEST_APPLICATIONS.append(app)
    return app


def _register_worker_session(
    app: DaemonApplication, actor: str, session_ref: str
) -> None:
    name = actor.rsplit(":", 1)[-1]
    spec = HarnessLaunchSpec(
        harness="claude", name=name, headless=True, session_ref=session_ref
    )
    app.desired_state.upsert_harness(spec)
    assert isinstance(app._harnesses, _FakeHarnesses)
    app._harnesses.refs[(spec.harness, spec.name)] = session_ref


def test_canonical_uri_matches_the_actor_registrar(tmp_path: Path) -> None:
    """The channel actor must equal the URI the registrar advertises."""

    app = _daemon(tmp_path)
    assert app._canonical_harness_uri("kanban-watcher") == CANONICAL


def test_whoami_returns_the_worker_actor_for_a_registered_worker(
    tmp_path: Path,
) -> None:
    app = _daemon(tmp_path)
    channel = build_worker_channel(
        actor=app._canonical_harness_uri("kanban-watcher"),
        session_ref="sess-1",
        hyprial_home=app.hyprial_home,
        state_dir=app.state_dir,
    )
    _register_worker_session(app, channel.actor, channel.session_ref)

    result = app.handle(
        "identity.whoami", {"actor": channel.actor, "sessionRef": "sess-1"}
    )

    assert result["actor"] == CANONICAL
    assert result["nodeId"] == "node-1"
    assert result["sessionRegistered"] is True


def test_whoami_reports_unregistered_for_a_mismatched_session(tmp_path: Path) -> None:
    app = _daemon(tmp_path)
    _register_worker_session(app, CANONICAL, "sess-1")

    result = app.handle(
        "identity.whoami", {"actor": CANONICAL, "sessionRef": "stale"}
    )

    # whoami is deliberately unfenced -- it answers, but says "not you".
    assert result["actor"] == CANONICAL
    assert result["sessionRegistered"] is False


def test_worker_session_passes_the_send_fence(tmp_path: Path) -> None:
    app = _daemon(tmp_path)
    _register_worker_session(app, CANONICAL, "sess-1")

    # message.pending.list runs the same fence as message.send; a matching
    # worker session gets past it to the (empty) inbox read.
    result = app.handle(
        "message.pending.list", {"actor": CANONICAL, "sessionRef": "sess-1"}
    )
    assert result == {"ok": True, "messages": [], "daemonEpoch": app.epoch}


def test_mismatched_worker_session_is_still_rejected(tmp_path: Path) -> None:
    app = _daemon(tmp_path)
    _register_worker_session(app, CANONICAL, "sess-1")

    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "message.pending.list", {"actor": CANONICAL, "sessionRef": "stale"}
        )
    assert raised.value.code == "STALE_SESSION"


def test_worker_sessions_do_not_change_interactive_fencing(tmp_path: Path) -> None:
    """Regression guard for the LIVE coordinator: interactive fencing intact."""

    app = _daemon(tmp_path)
    app.handle(
        "session.register",
        {
            "actor": "hq-adjutant",
            "sessionRef": "coord-1",
            "cwd": "/work",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )

    # The coordinator's own session still passes.
    assert app.handle(
        "message.pending.list", {"actor": "hq-adjutant", "sessionRef": "coord-1"}
    ) == {"ok": True, "messages": [], "daemonEpoch": app.epoch}

    # A worker session for a DIFFERENT actor never satisfies the coordinator's
    # fence, and a superseded coordinator ref is still rejected -- as the terminal
    # supersede verdict, since a different interactive session owns the actor.
    _register_worker_session(app, CANONICAL, "sess-1")
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(
            "message.pending.list", {"actor": "hq-adjutant", "sessionRef": "stale"}
        )
    assert raised.value.code == "SESSION_SUPERSEDED"


def test_all_worker_tools_are_pre_authorized() -> None:
    channel = _worker_channel()
    assert channel.allowed_tools == tuple(
        f"mcp__harness-bridge__{tool}" for tool in WORKER_HARNESS_TOOLS
    ) + WORKER_CLI_ALLOW_TOOLS


def test_agent_channel_entry_accepts_the_injected_args() -> None:
    """The mcp-config argv must match the entry signature the worker will invoke."""

    parser = mcp_entry._build_parser()
    parsed = parser.parse_args(["agent-channel", "--actor", CANONICAL])
    assert parsed.actor == CANONICAL
    assert parsed.session_ref is None
    # The session ref rides the environment for managed workers; the flag stays
    # accepted but unadvertised, exactly as the old hidden CLI option was.
    explicit = parser.parse_args(
        ["agent-channel", "--actor", CANONICAL, "--session-ref", "sess-xyz"]
    )
    assert explicit.session_ref == "sess-xyz"
