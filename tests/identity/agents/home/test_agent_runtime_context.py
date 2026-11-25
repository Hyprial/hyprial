"""P2 runtime context, projection, environment, and capability checks for agy."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.identity.impl.agents import (
    AgentConfig,
    AgentConfigError,
    AgentRegistry,
    AgentRuntimeMigrationBindings,
    build_native_projection,
)
from hyprial.identity.impl.agents.home.environment import (
    apply_runtime_environment_profile,
    compose_worker_child_launch,
)
from hyprial.identity.impl.agents.home.native_credential import (
    install_native_credential_set,
)
from hyprial.identity.impl.agents.migration._base import MigrationEntry
from hyprial.identity.impl.agents.runtime.context import (
    agent_home_mode,
    resolve_agent_runtime_context,
)
from hyprial.identity.impl.agents.state.worker_proxy import model_vendor_family
from hyprial.identity.impl.users.runtime_capabilities import RUNTIME_HARNESSES, RuntimeCapability
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def _create_agy_registry(tmp_path: Path) -> tuple[AgentRegistry, Path]:
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir(parents=True)
    config_source = tmp_path / "personality"
    native_cfg = config_source / "native" / "agy" / "config.json"
    native_cfg.parent.mkdir(parents=True, exist_ok=True)
    native_cfg.write_text('{"model": "gemini-3.8-flash"}\n', encoding="utf-8")
    skill = config_source / "shared" / "skills" / "p2" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# P2\n", encoding="utf-8")

    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="agy-owner",
        machine="agy-node",
        hyprial_home=hyprial_home,
    )
    registry.create(
        "agy-worker",
        cwd=str(tmp_path / "work"),
        config={
            "sources": [{"path": str(config_source), "required": True}],
            "discovery": "explicit-only",
        },
    )
    registry.ensure_home("agy-worker")
    return registry, hyprial_home


def test_agy_projection_rejects_credential_path(tmp_path: Path) -> None:
    source = tmp_path / "C"
    token = source / "native" / "agy" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("SYNTHETIC_NOT_A_SECRET\n", encoding="utf-8")

    with pytest.raises(AgentConfigError, match="authentication must remain outside C"):
        AgentConfig(str(source)).freeze_manifest()


def test_agy_p2_runtime_context_omits_native_root_env_and_captures_launch(
    tmp_path: Path,
) -> None:
    registry, hyprial_home = _create_agy_registry(tmp_path)
    try:
        agent = registry.require("agy-worker")
        assert agent_home_mode(agent, "agy") == ("p2", None)
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="agy-worker",
            harness="agy",
            cwd=agent.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        assert context.harness == "agy"
        agent_home = Path(registry.home_receipt("agy-worker").path)
        assert context.roots.native_root == agent_home / "secrets" / "native" / "agy"
        assert context.roots.session_root == agent_home / "state" / "sessions" / "agy"

        env = context.environment()
        assert "" not in env
        assert env["HOME"] == str(context.roots.tool_home)
        assert "CLAUDE_CONFIG_DIR" not in env
        assert "CODEX_HOME" not in env
        assert "PI_CODING_AGENT_DIR" not in env

        applied = apply_runtime_environment_profile(
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/ambient/home",
                "CLAUDE_CONFIG_DIR": "/ambient/claude",
            },
            env,
        )
        assert "" not in applied
        assert applied["HOME"] == str(context.roots.tool_home)
        assert "CLAUDE_CONFIG_DIR" not in applied

        install_native_credential_set(
            agent_home,
            "agy",
            [("antigravity-oauth-token", b'{"access_token":"synthetic"}\n')],
            import_record=registry.native_credential_import_record("agy-worker"),
        )
        channel = build_worker_channel(
            actor=agent.uri,
            session_ref="agy-session",
            hyprial_home=hyprial_home,
            state_dir=tmp_path / "daemon-state",
            node_id="agy-node",
            owner="agy-owner",
            runtime_context=context,
        )
        launch = compose_worker_child_launch(
            registry=registry,
            hyprial_home=hyprial_home,
            channel=channel,
            environ={"PATH": "/usr/bin:/bin"},
            agent_name="agy-worker",
            harness="agy",
        )
        assert launch.native_auth_launch is not None
    finally:
        registry.close()


def test_agy_profile_capability_and_worker_proxy_vendor() -> None:
    assert "agy" in RUNTIME_HARNESSES
    parsed = RuntimeCapability.from_json(
        {
            "harness": "agy",
            "model": "gemini-3.8-flash",
            "status": "available",
            "probedAt": "2026-11-21T00:00:00Z",
        },
        "capability",
    )
    assert parsed.harness == "agy"
    assert model_vendor_family("agy", None) == "google"
    assert model_vendor_family("agy", "google") == "google"


def test_agy_projection_loads_config_and_shared_skills(tmp_path: Path) -> None:
    source = tmp_path / "C"
    cfg = source / "native" / "agy" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}\n", encoding="utf-8")
    skill = source / "shared" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# review\n", encoding="utf-8")

    manifest = AgentConfig(str(source)).freeze_manifest()
    projection = build_native_projection(manifest, "agy")
    assert {item.native_path for item in projection.items} == {
        "config.json",
        "skills/review/SKILL.md",
    }


def test_agy_phase1_migration_closed_set_rejects_agy(tmp_path: Path) -> None:
    registry, _ = _create_agy_registry(tmp_path)
    try:
        with pytest.raises(
            ValueError, match="migration runtime harness must be claude, codex, or pi"
        ):
            AgentRuntimeMigrationBindings(
                registry,
                harnesses=("agy",),
                tool_profile=TEST_TOOL_PROFILE,
                base_environment={},
            )
    finally:
        registry.close()

    with pytest.raises(
        ValueError, match="unsupported migration destination: secrets/native/agy"
    ):
        MigrationEntry(str(tmp_path / "src"), "secrets/native/agy", True)
