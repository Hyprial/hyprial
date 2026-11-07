"""A resident pi agent refuses to start without its granted provider credential.

Field shape (2026-10-16): agents created and started before any credential
grant ran pi secretless; pi failed ``PROVIDER_AUTHENTICATION_FAILED`` and the
daemon blocked them credential-invalid.  Granting ``DEEPSEEK_API_KEY``,
restarting and unblocking recovered them, so the credential source is and
stays the per-agent authorization.

The gap these tests close: pi -- unlike claude and codex -- had no provider
step at its launch boundary, so a launch with no grant was not refused; it ran,
failed its first turns and got blocked.  The fix refuses at the launch
boundary.  No test here invents an operator/global credential source.

Every credential is a fixed test sentinel; no real key is read or written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch
from hyprial.identity.impl.agents.runtime import (
    resolve_agent_runtime_context,
)
from hyprial.identity import AgentSecretStore
from hyprial.identity.impl.agents.runtime.secrets import SecretSource
from hyprial.kernel import HarnessLaunchSpec
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

SENTINEL_KEY = "sk-resident-pi-provider-sentinel"
NODE = shutil.which("node")
PI = shutil.which("pi")


def _operator_home(tmp_path: Path) -> Path:
    """An isolated operator home whose Pi auth.json holds a fixed fake key."""

    home = tmp_path / "operator-home"
    directory = home / ".pi" / "agent"
    directory.mkdir(parents=True)
    (directory / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": SENTINEL_KEY}}) + "\n",
        encoding="utf-8",
    )
    return home


def _p2_pi_context(tmp_path: Path):
    repository = tmp_path / "repo"
    cwd = repository / "nested"
    cwd.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    personality = tmp_path / "personality"
    settings = personality / "native" / "pi" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        '{"defaultProvider":"openai","defaultModel":"gpt-4o-mini"}\n',
        encoding="utf-8",
    )
    (personality / "native" / "pi" / "AGENTS.md").write_text(
        "PI-CRED\n", encoding="utf-8"
    )
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="pi-cred-owner",
        machine="pi-cred-node",
        hyprial_home=hyprial_home,
    )
    registry.create(
        "worker",
        cwd=str(cwd),
        config={
            "sources": [{"path": str(personality), "required": True}],
            "discovery": "explicit-only",
        },
    )
    registry.ensure_home("worker")
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness="pi",
        cwd=str(cwd),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    return registry, hyprial_home, repository, cwd, context


def _grant_deepseek(registry: AgentRegistry, hyprial_home: Path) -> None:
    store = AgentSecretStore(Path(registry.home_receipt("worker").path))
    store.set(
        service="deepseek.json", field="DEEPSEEK_API_KEY", value=SENTINEL_KEY,
        actor="user:owner", via="cli", at_ms=1,
    )
    registry.grant_secret(
        "worker",
        grant_id="deepseek",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="deepseek.json",
        field_name="DEEPSEEK_API_KEY",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )


def _complete_launch(registry, hyprial_home, context, tmp_path):
    from hyprial.daemon.impl.harnesses.turn_delivery.channel.worker_channel import (
        build_worker_channel,
    )

    channel = build_worker_channel(
        actor=context.actor,
        session_ref="pi-cred-channel",
        hyprial_home=hyprial_home,
        state_dir=tmp_path / "daemon-state",
        runtime_context=context,
    )
    return channel, compose_worker_child_launch(
        registry=registry,
        hyprial_home=hyprial_home,
        channel=channel,
        environ={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/ambient"},
        agent_name="worker",
    )


def _deepseek_spec(cwd: Path) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        "pi",
        "worker",
        True,
        args=("--approve",),
        cwd=str(cwd),
        model_provider="deepseek",
        model="deepseek-flash",
    )


def _deepseek_config_context(
    tmp_path: Path, *, provider: str = "deepseek", model: str = "deepseek-flash"
):
    """A P2 pi context whose agent configuration (C) defaults to ``provider``.

    Mirrors the acceptance probe: the C source is rewritten and the context is
    re-resolved, so the native projection carries the new settings default.
    """

    registry, hyprial_home, repository, cwd, _stale = _p2_pi_context(tmp_path)
    settings = tmp_path / "personality" / "native" / "pi" / "settings.json"
    settings.write_text(
        json.dumps({"defaultProvider": provider, "defaultModel": model}) + "\n",
        encoding="utf-8",
    )
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness="pi",
        cwd=str(cwd),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    return registry, hyprial_home, repository, cwd, context


def test_pi_provider_environment_refuses_a_secretless_config_default_launch(
    tmp_path: Path,
) -> None:
    """R1: the effective provider from the agent configuration is checked.

    Acceptance scenario (2026-10-16): the C source selects deepseek, the launch
    names no provider, no key is granted and the native root has no auth.json.
    The loader then picked deepseek from the settings and reached ready with
    ``model.provider=unknown``.  The credential boundary must resolve the same
    effective provider and refuse before launch, exactly as the explicit
    ``--provider deepseek`` path does.
    """

    from hyprial.daemon import pi_provider_environment
    from hyprial.daemon.impl.harnesses.model_provider import (
        AgentProviderCredentialMissing,
    )

    registry, hyprial_home, _repository, cwd, context = _deepseek_config_context(
        tmp_path
    )
    try:
        _channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        environment = complete.environment.for_exec()
        assert "DEEPSEEK_API_KEY" not in environment
        with pytest.raises(
            AgentProviderCredentialMissing, match="agent-owned DEEPSEEK_API_KEY"
        ):
            pi_provider_environment(
                replace(_deepseek_spec(cwd), model_provider=None, model=None),
                environment,
                native_root=context.roots.native_root,
                settings_root=context.roots.projection_root,
                allow_legacy_home_fallback=False,
            )
    finally:
        registry.close()


def test_pi_provider_environment_honors_a_granted_config_default(
    tmp_path: Path,
) -> None:
    """R1: the config-default path still launches once the grant exists."""

    from hyprial.daemon import pi_provider_environment

    registry, hyprial_home, _repository, cwd, context = _deepseek_config_context(
        tmp_path
    )
    try:
        _grant_deepseek(registry, hyprial_home)
        _channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        resolved = pi_provider_environment(
            replace(_deepseek_spec(cwd), model_provider=None, model=None),
            complete.environment.for_exec(),
            native_root=context.roots.native_root,
            settings_root=context.roots.projection_root,
            allow_legacy_home_fallback=False,
        )
        assert resolved == {"DEEPSEEK_API_KEY": SENTINEL_KEY}
    finally:
        registry.close()


def test_pi_provider_environment_leaves_a_builtin_config_default_alone(
    tmp_path: Path,
) -> None:
    """R1 negative: a built-in vendor default needs no hyprial credential."""

    from hyprial.daemon import pi_provider_environment

    # ``_p2_pi_context`` writes defaultProvider=openai, defaultModel=gpt-4o-mini.
    registry, hyprial_home, _repository, cwd, context = _deepseek_config_context(
        tmp_path, provider="openai", model="gpt-4o-mini"
    )
    try:
        _channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        assert (
            pi_provider_environment(
                replace(_deepseek_spec(cwd), model_provider=None, model=None),
                complete.environment.for_exec(),
                native_root=context.roots.native_root,
                settings_root=context.roots.projection_root,
                allow_legacy_home_fallback=False,
            )
            == {}
        )
    finally:
        registry.close()


def test_pi_rpc_process_refuses_a_config_default_launch_without_a_credential(
    tmp_path: Path,
) -> None:
    """R1: the same check refuses the resident worker before it can be ready.

    A fresh session must never appear ready with ``model.provider=unknown``
    because no credential was checked: the process driver resolves the
    effective provider (C default here) and refuses at composition time.
    """

    from hyprial.daemon.impl.harnesses.model_provider import (
        AgentProviderCredentialMissing,
    )
    from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

    registry, hyprial_home, _repository, cwd, context = _deepseek_config_context(
        tmp_path
    )
    try:
        channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        with pytest.raises(AgentProviderCredentialMissing):
            PiRpcProcess(
                replace(_deepseek_spec(cwd), model_provider=None, model=None),
                worker_channel=channel,
                complete_launch=complete,
            )
    finally:
        registry.close()


def test_pi_rpc_process_carries_the_local_provider_endpoint_into_p2_loader(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

    registry, hyprial_home, _repository, cwd, context = _p2_pi_context(tmp_path)
    process = None
    try:
        _grant_deepseek(registry, hyprial_home)
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=str(cwd),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        complete = replace(
            complete,
            environment=type(complete.environment)(
                tuple(
                    {
                        **complete.environment.for_exec(),
                        "HYPRIAL_OPENAI_BASE_URL": "http://127.0.0.1:54321/v1",
                    }.items()
                )
            ),
        )
        process = PiRpcProcess(
            _deepseek_spec(cwd),
            worker_channel=channel,
            complete_launch=complete,
        )

        assert process._provider_environment["HYPRIAL_OPENAI_BASE_URL"] == (
            "http://127.0.0.1:54321/v1"
        )
    finally:
        if process is not None:
            process.stop()
        registry.close()


def test_pi_provider_environment_prefers_the_granted_launch_key(
    tmp_path: Path,
) -> None:
    from hyprial.daemon import pi_provider_environment

    spec = _deepseek_spec(tmp_path)
    environment = pi_provider_environment(
        spec,
        {"DEEPSEEK_API_KEY": SENTINEL_KEY},
        allow_legacy_home_fallback=False,
    )
    assert environment == {"DEEPSEEK_API_KEY": SENTINEL_KEY}


def test_pi_provider_environment_allows_the_legacy_home_fallback_only_on_legacy(
    tmp_path: Path,
) -> None:
    from hyprial.daemon import pi_provider_environment

    operator_home = _operator_home(tmp_path)
    spec = _deepseek_spec(tmp_path)
    legacy = pi_provider_environment(
        spec,
        {"HOME": str(operator_home)},
        allow_legacy_home_fallback=True,
    )
    assert legacy == {"DEEPSEEK_API_KEY": SENTINEL_KEY}
    # The same operator key must NOT be reachable for a P2 launch.
    with pytest.raises(Exception, match="agent-owned DEEPSEEK_API_KEY"):
        pi_provider_environment(
            spec,
            {"HOME": str(operator_home)},
            allow_legacy_home_fallback=False,
        )


def test_pi_provider_environment_leaves_an_agent_owned_credential_in_place(
    tmp_path: Path,
) -> None:
    """A shared-link/native credential wins; pi's loader reads it directly."""

    from hyprial.daemon import pi_provider_environment

    native_root = tmp_path / "native"
    native_root.mkdir()
    (native_root / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": "sk-agent-owned"}}) + "\n",
        encoding="utf-8",
    )
    assert (
        pi_provider_environment(
            _deepseek_spec(tmp_path),
            {"HOME": str(tmp_path / "agent-tool-home")},
            native_root=native_root,
            allow_legacy_home_fallback=False,
        )
        == {}
    )


def test_pi_provider_environment_refuses_a_secretless_p2_launch(
    tmp_path: Path,
) -> None:
    from hyprial.daemon import pi_provider_environment
    from hyprial.daemon.impl.harnesses.model_provider import (
        AgentProviderCredentialMissing,
    )

    operator_home = _operator_home(tmp_path)
    with pytest.raises(AgentProviderCredentialMissing):
        # Even with an operator key present: a P2 agent gets no credential it
        # was not granted.
        pi_provider_environment(
            _deepseek_spec(tmp_path),
            {"HOME": str(tmp_path / "agent-tool-home")},
            allow_legacy_home_fallback=False,
        )
    assert operator_home.is_dir()


def test_credential_invalid_block_persists_until_explicit_unblock(
    tmp_path: Path,
) -> None:
    """Granting a credential does not clear the block the failure created.

    Field shape (2026-10-16): the first failed turns blocked be-dev-1/2/3 as
    credential-invalid; granting DEEPSEEK_API_KEY did not clear the block, and
    the operator had to run `agent unblock` after restarting.  This pins that
    the block is explicit state, not a derived view of the grants.
    """

    registry, hyprial_home, _repository, _cwd, _context = _p2_pi_context(tmp_path)
    try:
        block, changed = registry.block_agent("worker", reason="credential-invalid")
        assert changed is True and block is not None
        assert registry.is_blocked("worker") is True
        _grant_deepseek(registry, hyprial_home)
        assert registry.is_blocked("worker") is True
        assert registry.unblock_agent("worker") is True
        assert registry.is_blocked("worker") is False
    finally:
        registry.close()


@pytest.mark.skipif(
    NODE is None or PI is None,
    reason="the real node and installed Pi SDK are required",
)
def test_p2_pi_complete_environment_carries_a_granted_credential(
    tmp_path: Path,
) -> None:
    """The daemon composition plus the resolver yield pi's credential env."""

    from hyprial.daemon import pi_provider_environment

    registry, hyprial_home, _repository, cwd, context = _p2_pi_context(tmp_path)
    try:
        # Before the grant: the complete environment has no provider key.
        _channel, unsupplied = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        assert "DEEPSEEK_API_KEY" not in unsupplied.environment.for_exec()
        _grant_deepseek(registry, hyprial_home)
        channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        assert complete.environment.for_exec()["DEEPSEEK_API_KEY"] == SENTINEL_KEY
        resolved = pi_provider_environment(
            _deepseek_spec(cwd),
            complete.environment.for_exec(),
            native_root=context.roots.native_root,
            allow_legacy_home_fallback=False,
        )
        assert resolved == {"DEEPSEEK_API_KEY": SENTINEL_KEY}
        assert channel.runtime_context is context
    finally:
        registry.close()


@pytest.mark.skipif(
    NODE is None or PI is None,
    reason="the real node and installed Pi SDK are required",
)
def test_pi_spawn_environment_merges_the_provider_credential(
    tmp_path: Path,
) -> None:
    """The carrier hands the resolved key to the pi process, keeping the rest."""

    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    registry, hyprial_home, _repository, cwd, context = _p2_pi_context(tmp_path)
    try:
        channel, complete = _complete_launch(
            registry, hyprial_home, context, tmp_path
        )
        client = PiRpcClient(
            _deepseek_spec(cwd),
            "pi-cred-session",
            command=(PI or "pi",),
            worker_channel=channel,
            complete_launch=complete,
            provider_environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
        )
        spawn = client._spawn_environment()
        assert spawn is not None
        assert spawn["DEEPSEEK_API_KEY"] == SENTINEL_KEY
        # Nothing else is replaced by the credential merge.
        frozen = complete.environment.for_exec()
        assert frozen.items() <= spawn.items()
        assert spawn["PI_CODING_AGENT_DIR"] == str(context.roots.native_root)
    finally:
        registry.close()
