from __future__ import annotations

import stat
from pathlib import Path

from hyprial.daemon.impl.harnesses.codex import prepare_codex_runtime_context
from hyprial.identity.impl.agents import (
    AgentRegistry,
    resolve_agent_runtime_context,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def _registry_with_codex_config(
    tmp_path: Path,
    *,
    provider: str | None,
    shared_target: Path | None = None,
) -> tuple[AgentRegistry, Path]:
    source = tmp_path / "personality"
    config = source / "native" / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('cli_auth_credentials_store = "file"\nmodel = "test"\n')
    home = tmp_path / "hyprial"
    home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=home,
    )
    agent_config: dict[str, object] = {
        "sources": [{"path": str(source), "required": True}],
        "discovery": "explicit-only",
    }
    if shared_target is not None:
        agent_config["sharedCredentials"] = {"codex": str(shared_target)}
    registry.create(
        "worker",
        cwd=str(tmp_path / "workspace"),
        config=agent_config,
        provider=provider,
    )
    registry.ensure_home("worker")
    return registry, config


def _context(registry: AgentRegistry):  # type: ignore[no-untyped-def]
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness="codex",
        cwd=registry.require("worker").cwd,
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    return context


def test_custom_provider_reconciles_seeded_openai_auth_non_destructively(
    tmp_path: Path,
) -> None:
    registry, source_config = _registry_with_codex_config(
        tmp_path, provider="deepseek"
    )
    try:
        context = _context(registry)
        auth = context.roots.native_root / "auth.json"
        auth.write_bytes(b'{"OPENAI_API_KEY":"test-only"}\n')
        auth.chmod(0o600)

        prepared = prepare_codex_runtime_context(context)

        assert prepared.model_provider == "deepseek"
        assert not auth.exists()
        assert 'cli_auth_credentials_store = "ephemeral"' in (
            context.roots.native_root / "config.toml"
        ).read_text()
        assert source_config.read_text().startswith(
            'cli_auth_credentials_store = "file"'
        )
        assert prepared.shared_credential is None
        assert len(prepared.notices) == 1
        notice = prepared.notices[0]
        backup = Path(notice.path)
        assert notice.code == "CODEX_CUSTOM_PROVIDER_AUTH_BACKUP"
        assert notice.actor == context.actor
        assert backup.read_bytes() == b'{"OPENAI_API_KEY":"test-only"}\n'
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert backup.is_relative_to(context.roots.agent_home / "state")

        repeated = prepare_codex_runtime_context(prepared)
        assert repeated.notices == prepared.notices
        assert not auth.exists()

        registry.update("worker", provider="openai")
        openai_context = _context(registry)
        auth.write_bytes(b'{"OPENAI_API_KEY":"new-test-only"}\n')
        auth.chmod(0o600)
        repaired = prepare_codex_runtime_context(openai_context)
        assert repaired.model_provider == "openai"
        assert auth.read_bytes() == b'{"OPENAI_API_KEY":"new-test-only"}\n'
        assert 'cli_auth_credentials_store = "file"' in (
            context.roots.native_root / "config.toml"
        ).read_text()
    finally:
        registry.close()


def test_custom_provider_preserves_designated_shared_auth_as_regular_backup(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared-auth.json"
    shared.write_bytes(b'{"OPENAI_API_KEY":"shared-test-only"}\n')
    shared.chmod(0o600)
    registry, _ = _registry_with_codex_config(
        tmp_path, provider="deepseek", shared_target=shared
    )
    try:
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
        auth.symlink_to(shared)

        prepared = prepare_codex_runtime_context(_context(registry))

        assert not auth.exists()
        assert prepared.shared_credential is None
        backup = Path(prepared.notices[0].path)
        assert not backup.is_symlink()
        assert backup.read_bytes() == shared.read_bytes()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    finally:
        registry.close()


def test_openai_keeps_file_credentials_and_file_store(tmp_path: Path) -> None:
    registry, _ = _registry_with_codex_config(tmp_path, provider="openai")
    try:
        context = _context(registry)
        auth = context.roots.native_root / "auth.json"
        auth.write_bytes(b'{"OPENAI_API_KEY":"test-only"}\n')
        auth.chmod(0o600)

        prepared = prepare_codex_runtime_context(context)

        assert auth.is_file()
        assert auth.read_bytes() == b'{"OPENAI_API_KEY":"test-only"}\n'
        assert 'cli_auth_credentials_store = "file"' in (
            context.roots.native_root / "config.toml"
        ).read_text()
        assert prepared.notices == ()
    finally:
        registry.close()
