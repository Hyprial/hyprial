"""agent_home_p2_migration: projection scenarios.

Fix round 1 (review of f98c63e248, R4): the upgrade path must surface the
one-shot migration notice through an injectable reporter so the owning
daemon can log it, exactly once per real migration and never on repeat.
"""

from __future__ import annotations
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import (
    AgentConfig,
    AgentRuntimeMigrationBindings,
    build_native_projection,
)
from hyprial.identity.impl.agents.migration._base import (
    AgentMigrationAuthorization,
    MigrationEntry,
    MigrationPhase,
    SupportRow,
    SupportStatus,
)
from hyprial.identity.impl.agents.runtime.context import resolve_agent_runtime_context
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from tests.identity.agents.runtime.test_git_identity_migration import RETIRED_PROFILE

from .support import (
    _authorization,
    _coordinator,
    _isolated_environment as _isolated_environment,
    _registry,
    _source,
    _support,
    _support_key,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_t08_three_harness_projections_share_exactly_one_frozen_c(tmp_path: Path) -> None:
    source = tmp_path / "config"
    for harness in ("claude", "codex", "pi"):
        path = source / "native" / harness
        path.mkdir(parents=True)
        (path / "settings.txt").write_text(f"{harness}-setting\n", encoding="utf-8")
    shared = source / "shared" / "skills" / "portable"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("portable skill\n", encoding="utf-8")

    config = AgentConfig(str(source))
    manifest = config.freeze_manifest()
    projections = tuple(
        build_native_projection(manifest, harness) for harness in ("claude", "codex", "pi")
    )

    assert {projection.source_digest for projection in projections} == {manifest.digest}
    assert all(
        any(item.native_path == "skills/portable/SKILL.md" for item in projection.items)
        for projection in projections
    )
    changed = source / "shared" / "skills" / "portable" / "SKILL.md"
    changed.write_text("drift\n", encoding="utf-8")
    with pytest.raises(Exception, match="config changed"):
        config.verify_manifest(manifest)


def test_t08_three_harnesses_use_one_real_p22_context_and_worker_channel(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        agent = registry.create("worker", cwd=str(tmp_path / "workspace"))
        source = _source(tmp_path)
        keys = tuple(_support_key(harness) for harness in ("claude", "codex", "pi"))
        matrix = _support(
            *(
                SupportRow(key, SupportStatus.PASS, "isolated P22 context fixture")
                for key in keys
            )
        )
        ambient = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/ambient/home",
            "XDG_CONFIG_HOME": "/ambient/xdg",
            "SSH_AUTH_SOCK": "/ambient/agent.sock",
            "CLAUDE_CONFIG_DIR": "/ambient/claude",
            "CODEX_HOME": "/ambient/codex",
            "PI_CODING_AGENT_DIR": "/ambient/pi",
        }
        bindings = AgentRuntimeMigrationBindings(
            registry,
            harnesses=("claude", "codex", "pi"),
            tool_profile=TEST_TOOL_PROFILE,
            base_environment=ambient,
        )
        coordinator = _coordinator(registry, matrix)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            required_support=keys,
            bindings=bindings,
        )
        completed = coordinator.execute(plan, bindings=bindings)

        assert completed.phase is MigrationPhase.COMPLETE
        assert len(bindings.contexts) == 3
        assert {context.actor for context in bindings.contexts} == {agent.uri}
        assert {context.entity_token for context in bindings.contexts} == {
            agent.entity_token
        }
        assert len({context.config_revision for context in bindings.contexts}) == 1
        config_sources = {context.roots.config_source for context in bindings.contexts}
        assert config_sources == {
            Path(registry.home_receipt("worker").path) / "config"
        }
        assert len({context.roots.native_root for context in bindings.contexts}) == 3

        for context in bindings.contexts:
            environment = bindings.environments[context.harness]
            assert environment["HOME"] == str(context.roots.tool_home)
            assert environment["XDG_CONFIG_HOME"] == str(
                context.roots.xdg_config_home
            )
            assert environment.get("SSH_AUTH_SOCK") is None
            channel = build_worker_channel(
                actor=agent.uri,
                session_ref=f"p24-{context.harness}",
                hyprial_home=tmp_path / "hyprial-home",
                state_dir=tmp_path / "daemon-state",
                node_id="node",
                owner="owner",
                runtime_context=context,
            )
            assert channel.runtime_context is context

        fresh = AgentMigrationAuthorization(
            plan.authorization.actor,
            plan.authorization.entity_token,
            plan.authorization.home_resource_token,
            "authorized-window-rollback",
            "operator@example.test",
            2_000,
        )
        rolled_back = coordinator.rollback(
            plan, authorization=fresh, bindings=bindings
        )
        assert rolled_back.phase is MigrationPhase.ROLLED_BACK
        assert bindings.contexts == ()
        assert registry.require("worker").config is None
        assert source.exists()
    finally:
        registry.close()


def test_codex_migration_activates_provider_aware_home_reconciliation(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.harnesses.codex import prepare_codex_runtime_context

    registry = _registry(tmp_path)
    try:
        source = tmp_path / "codex-config"
        config = source / "native" / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('cli_auth_credentials_store = "file"\n')
        registry.create(
            "worker",
            cwd=str(tmp_path / "workspace"),
            provider="deepseek",
        )
        native = (
            Path(registry.home_receipt("worker").path)
            / "secrets"
            / "native"
            / "codex"
        )
        native.mkdir(parents=True)
        native.parent.parent.chmod(0o700)
        native.parent.chmod(0o700)
        native.chmod(0o700)
        auth = native / "auth.json"
        auth.write_bytes(b'{"OPENAI_API_KEY":"test-only"}\n')
        auth.chmod(0o600)
        key = _support_key("codex")
        bindings = AgentRuntimeMigrationBindings(
            registry,
            harnesses=("codex",),
            tool_profile=TEST_TOOL_PROFILE,
            base_environment={"PATH": "/usr/bin:/bin", "HOME": "/ambient"},
            runtime_preparers={"codex": prepare_codex_runtime_context},
        )
        coordinator = _coordinator(
            registry,
            _support(SupportRow(key, SupportStatus.PASS, "provider-aware fixture")),
        )
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            required_support=(key,),
            bindings=bindings,
        )

        coordinator.execute(plan, bindings=bindings)

        context = bindings.contexts[0]
        assert context.model_provider == "deepseek"
        assert not auth.exists()
        assert Path(context.notices[0].path).read_bytes() == (
            b'{"OPENAI_API_KEY":"test-only"}\n'
        )
        assert 'cli_auth_credentials_store = "ephemeral"' in (
            native / "config.toml"
        ).read_text()
    finally:
        registry.close()


def test_migration_bindings_report_the_notice_once_and_never_on_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    try:
        source = _source(tmp_path)
        registry.create(
            "worker",
            cwd=str(tmp_path / "workspace"),
            config=AgentConfig(str(source)),
        )
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        (owner_home / ".gitconfig").write_text(
            "[user]\n\tname = Owner Person\n\temail = owner@example.test\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(owner_home))
        legacy = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="pi",
            cwd=str(tmp_path / "workspace"),
            tool_profile=RETIRED_PROFILE,
        )
        assert legacy is not None

        reported: list[object] = []

        def report(context: object) -> None:
            reported.extend(
                notice
                for notice in getattr(context, "notices", ())
                if notice.code == "AGENT_GIT_PROFILE_MIGRATED"
            )

        key = _support_key("pi")
        bindings = AgentRuntimeMigrationBindings(
            registry,
            harnesses=("pi",),
            tool_profile=None,
            base_environment={"PATH": "/usr/bin:/bin", "HOME": str(owner_home)},
            notice_reporter=report,
        )
        coordinator = _coordinator(
            registry,
            _support(SupportRow(key, SupportStatus.PASS, "notice fixture")),
        )
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            required_support=(key,),
            bindings=bindings,
        )
        completed = coordinator.execute(plan, bindings=bindings)

        assert completed.phase is MigrationPhase.COMPLETE
        assert len(reported) == 1
        assert reported[0].actor == "worker"

        reported.clear()
        bindings.activate(plan)
        assert reported == []
    finally:
        registry.close()


def test_t15_new_binding_process_restores_durable_prior_config(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        source = _source(tmp_path)
        registry.create(
            "worker",
            cwd=str(tmp_path / "workspace"),
            config=AgentConfig(str(source)),
        )
        key = _support_key("codex")
        matrix = _support(
            SupportRow(key, SupportStatus.PASS, "isolated P22 context fixture")
        )

        def new_bindings() -> AgentRuntimeMigrationBindings:
            return AgentRuntimeMigrationBindings(
                registry,
                harnesses=("codex",),
                tool_profile=TEST_TOOL_PROFILE,
                base_environment={"PATH": "/usr/bin:/bin", "HOME": "/ambient"},
            )

        first_process = new_bindings()
        coordinator = _coordinator(registry, matrix)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            required_support=(key,),
            bindings=first_process,
        )
        coordinator.execute(plan, bindings=first_process)
        assert registry.require("worker").config != AgentConfig(str(source))

        second_process = new_bindings()
        fresh = AgentMigrationAuthorization(
            plan.authorization.actor,
            plan.authorization.entity_token,
            plan.authorization.home_resource_token,
            "authorized-window-rollback",
            "operator@example.test",
            2_000,
        )
        coordinator.rollback(plan, authorization=fresh, bindings=second_process)

        assert registry.require("worker").config == AgentConfig(str(source))
        assert source.exists()
    finally:
        registry.close()
