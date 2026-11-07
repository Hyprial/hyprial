"""P1 keeps the daemon's harness config roots; P2 still owns them.

``environment.py`` states the rule for HOME/XDG: they stay in BASE so the
unconfigured P1 mode preserves the daemon's values, and a P2 composition
strips them and supplies its own.  The harness config roots
(CLAUDE_CONFIG_DIR, CODEX_HOME, PI_CODING_AGENT_DIR) are the same kind of
name but were only GENERATED, so since P1b B2 (13cd01d5) a P1 worker lost
them silently.  Observed: nightly e2e-controlled sets
CLAUDE_CONFIG_DIR=~/.claude-ci for its claude workers; from 2026-09-20 their
transcripts landed in ~/.claude instead, and E2E-006
claude.fresh-session-id was BLOCKED every night.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
    apply_runtime_environment_profile,
    compose_worker_child_launch,
    whitelist_replacement_environment,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

ROOTS = {
    "CLAUDE_CONFIG_DIR": "/Users/ci/.claude-ci",
    "CODEX_HOME": "/Users/ci/.codex-ci",
    "PI_CODING_AGENT_DIR": "/Users/ci/.pi-ci/agent",
}


def _p1_launch(
    name: str, value: Path, extra: dict[str, str] | None = None
) -> ChildEnvironmentLaunch:
    values = {
        "HOME": str(value.parent / "home"),
        "PATH": "/usr/bin:/bin",
        name: str(value),
        **(extra or {}),
    }
    return ChildEnvironmentLaunch(
        environment=CompleteChildEnvironment(tuple(sorted(values.items()))),
        actor="agent:roots:node:worker",
    )


def _spec(harness: str, tmp_path: Path) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness=harness,
        name=f"{harness}-worker",
        headless=True,
        cwd=str(tmp_path),
    )


def _compose(tmp_path: Path, environ: dict[str, str]) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    registry = AgentRegistry(
        state / "agents.sqlite3",
        owner="new-owner",
        machine="roots-node",
        hyprial_home=home,
    )
    try:
        registry.create("worker")
        channel = build_worker_channel(
            actor="agent:roots:node:worker",
            session_ref="sess-1",
            hyprial_home=home,
            state_dir=state,
            node_id="roots-node",
            owner="new-owner",
        )
        return compose_worker_child_launch(
            registry=registry,
            hyprial_home=home,
            channel=channel,
            environ={"PATH": "/usr/bin:/bin", **environ},
            agent_name="worker",
        ).environment.for_exec()
    finally:
        registry.close()


@pytest.mark.parametrize("name", sorted(ROOTS))
def test_p1_daemon_composition_keeps_the_daemons_config_root(tmp_path, name) -> None:
    environment = _compose(tmp_path, dict(ROOTS))
    assert environment.get(name) == ROOTS[name]


@pytest.mark.parametrize("name", sorted(ROOTS))
def test_p1_whitelist_keeps_the_config_root(name) -> None:
    environment = whitelist_replacement_environment(
        {"PATH": "/usr/bin:/bin", **ROOTS}
    )
    assert environment.get(name) == ROOTS[name]


def test_p1_absent_roots_stay_absent(tmp_path) -> None:
    """Nothing is invented: a daemon without the roots passes none."""

    environment = _compose(tmp_path, {})
    assert not set(ROOTS).intersection(environment)


def test_p2_profile_still_owns_the_config_roots() -> None:
    """Control: in P2 the profile's value wins, and an ambient root the
    profile does not set is dropped -- passing the roots through in P1
    must not leak the operator's roots into a P2 worker."""

    environment = apply_runtime_environment_profile(
        {"PATH": "/usr/bin:/bin", **ROOTS},
        {"CLAUDE_CONFIG_DIR": "/agents/worker/native/claude"},
    )
    assert environment["CLAUDE_CONFIG_DIR"] == "/agents/worker/native/claude"
    assert "CODEX_HOME" not in environment
    assert "PI_CODING_AGENT_DIR" not in environment


def test_p1_codex_launch_uses_the_operator_inherited_root(tmp_path: Path) -> None:
    native_root = tmp_path / "codex-operator-home"
    native_root.mkdir(mode=0o700)
    launch = _p1_launch("CODEX_HOME", native_root)

    class ProcessGroup:
        def register(self, _pid: int) -> None:
            return None

        def exists(self, _pid: int) -> bool:
            return False

        def release_if_gone(self, _pid: int) -> bool:
            return True

        def signal(self, _pid: int, _signum: int) -> None:
            return None

    client = CodexAppServerClient(
        _spec("codex", tmp_path),
        command=(
            sys.executable,
            str(Path(__file__).resolve().parents[3] / 'tests/fixtures/harnesses/fake_codex_app_server.py'),
        ),
        process_group=ProcessGroup(),  # type: ignore[arg-type]
        complete_launch=launch,
    )

    assert client._spawn_environment()["CODEX_HOME"] == str(native_root)
    assert client._native_root == native_root
    assert client._session_root == native_root / "sessions"

    async def start() -> None:
        async with client:
            assert client.session_ref == "codex-thread-1"

    asyncio.run(start())


def test_p1_claude_launch_uses_the_agent_native_root(tmp_path: Path) -> None:
    agent_home = tmp_path / "claude-agent-home"
    agent_home.mkdir(mode=0o700)
    native_root = agent_home / "secrets" / "native" / "claude"
    launch = _p1_launch(
        "CLAUDE_CONFIG_DIR",
        native_root,
        extra={"HYPRIAL_AGENT_DIR": str(agent_home), "ANTHROPIC_API_KEY": "test"},
    )

    client = IsolatedAgentSdkClient(
        _spec("claude", tmp_path),
        worker_command=("/fake/claude-worker",),
        session_id="session-1",
        complete_launch=launch,
    )

    assert client._runtime_context is None
    assert client._env["CLAUDE_CONFIG_DIR"] == str(native_root)


def test_p1_pi_launch_uses_the_operator_inherited_root(tmp_path: Path) -> None:
    native_root = tmp_path / "pi-operator-home"
    launch = _p1_launch("PI_CODING_AGENT_DIR", native_root)

    client = PiRpcClient(
        _spec("pi", tmp_path),
        "session-1",
        command=("/fake/pi",),
        complete_launch=launch,
    )

    assert client._spawn_environment()["PI_CODING_AGENT_DIR"] == str(native_root)
