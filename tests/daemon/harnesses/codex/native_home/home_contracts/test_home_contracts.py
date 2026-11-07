from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import signal
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import AgentActor, AgentRegistry
from hyprial.daemon.impl.harnesses.codex import process as codex_module
from hyprial.identity.impl.agents.home.config import AgentConfig, build_native_projection, materialize_native_projection
from hyprial.identity.impl.agents.home.environment import ChildEnvironmentLaunch, CompleteChildEnvironment
from hyprial.identity.impl.agents.runtime import AgentRuntimeError, SharedCredentialBinding, resolve_agent_runtime_context
from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel.impl.log import Logger
from hyprial.identity import load_packaged_support_matrix
from hyprial.daemon.impl.harnesses.codex import CodexAgentHomeError, CodexAppServerClient, CodexInteractiveAppServer, _thread_config, _validate_codex_native_load, prepare_codex_runtime_context, resolve_codex_executable, verify_codex_native_projection
from hyprial.daemon.impl.transfer.archive.session_files import SessionFileNotFound, codex_rollout_file_in_session_root, codex_rollout_target_in_session_root

from ..support import (
    _native_responses,
    _runtime_context,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_t04_t05_native_loader_proves_root_layers_project_and_skills(
    tmp_path: Path,
) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _native_responses(tmp_path)
    )

    evidence = _validate_codex_native_load(
        initialize=initialize,
        config_read=config_read,
        account_read=account_read,
        skills_list=skills,
        native_root=native_root,
        cwd=workspace,
        model_provider="openai",
    )

    assert evidence.codex_home == str(native_root)
    assert evidence.layer_types == ("project", "user", "system")
    assert evidence.effective_model == "p20-codex-project-sentinel"
    assert evidence.auth_store == "file"
    assert evidence.account_type == "apiKey"
    assert evidence.user_skills == ("p23-codex-user",)
    assert evidence.project_skills == ("p23-codex-project",)

    initialize["codexHome"] = str(tmp_path / "host" / ".codex")
    with pytest.raises(CodexAgentHomeError, match="resolved CODEX_HOME"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
        )
    initialize["codexHome"] = str(native_root)
    config_read["layers"][0]["name"]["dotCodexFolder"] = str(
        workspace.parent / ".codex"
    )
    with pytest.raises(CodexAgentHomeError, match="escaped the repository root"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
        )


def test_native_loader_accepts_only_its_prevalidated_shared_auth_link(
    tmp_path: Path,
) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _native_responses(tmp_path)
    )
    auth = native_root / "auth.json"
    target = tmp_path / "shared-codex-auth"
    target.write_bytes(auth.read_bytes())
    target.chmod(0o600)
    auth.unlink()
    auth.symlink_to(target)
    binding = SharedCredentialBinding(
        actor="worker",
        harness="codex",
        native_path=auth,
        target_path=target,
        agent_cwd=workspace,
    )

    evidence = _validate_codex_native_load(
        initialize=initialize,
        config_read=config_read,
        account_read=account_read,
        skills_list=skills,
        native_root=native_root,
        cwd=workspace,
        model_provider="openai",
        shared_credential=binding,
    )
    assert evidence.auth_store == "file"

    auth.unlink()
    auth.write_bytes(target.read_bytes())
    auth.chmod(0o600)
    with pytest.raises(AgentRuntimeError) as raised:
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
            shared_credential=binding,
        )
    assert raised.value.code == "SHARED_CREDENTIAL_DIVERGED"


def test_t05_project_layer_cannot_override_auth_or_channel_keys(tmp_path: Path) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _native_responses(tmp_path)
    )
    config_read["layers"][0]["config"]["cli_auth_credentials_store"] = "ephemeral"

    with pytest.raises(
        CodexAgentHomeError, match="project config.*cli_auth_credentials_store"
    ):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
        )

    del config_read["layers"][0]["config"]["cli_auth_credentials_store"]
    config_read["layers"][0]["config"]["mcp_servers"] = {
        "project-tool": {"command": "project-tool"}
    }
    _validate_codex_native_load(
        initialize=initialize,
        config_read=config_read,
        account_read=account_read,
        skills_list=skills,
        native_root=native_root,
        cwd=workspace,
        model_provider="openai",
    )
    config_read["layers"][0]["config"]["mcp_servers"]["harness-bridge"] = {
        "command": "attacker-controlled"
    }
    with pytest.raises(CodexAgentHomeError, match="reserved MCP server"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
        )


def test_t07_auth_modes_are_explicit_and_custom_provider_rejects_dual_auth(
    tmp_path: Path,
) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _native_responses(tmp_path)
    )
    for unsupported_store in ("auto", "keyring"):
        config_read["config"]["cli_auth_credentials_store"] = unsupported_store
        with pytest.raises(
            CodexAgentHomeError,
            match=f"credential store '{unsupported_store}'",
        ):
            _validate_codex_native_load(
                initialize=initialize,
                config_read=config_read,
                account_read=account_read,
                skills_list=skills,
                native_root=native_root,
                cwd=workspace,
                model_provider="openai",
            )

    config_read["config"]["cli_auth_credentials_store"] = "file"
    account_read["requiresOpenaiAuth"] = False
    config_read["layers"].insert(
        0,
        {
            "name": {"type": "sessionFlags"},
            "version": "P23_REAL_SHAPE_SENTINEL",
            "config": {
                "model": "deepseek-flash",
                "model_provider": "deepseek",
                "model_reasoning_effort": "high",
                "model_providers": {
                    "deepseek": {
                        "name": "deepseek",
                        "base_url": "https://api.deepseek.com/",
                        "wire_api": "responses",
                        "env_key": "DEEPSEEK_API_KEY",
                        "requires_openai_auth": False,
                    }
                },
            },
        },
    )
    with pytest.raises(CodexAgentHomeError, match="both native auth.json and custom"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="deepseek",
        )


def test_t08_codex_consumes_only_its_p21_projection_and_detects_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config"
    (source / "native" / "codex").mkdir(parents=True)
    (source / "native" / "codex" / "AGENTS.md").write_text("codex sentinel")
    (source / "native" / "claude").mkdir(parents=True)
    (source / "native" / "claude" / "CLAUDE.md").write_text("claude sentinel")
    config = AgentConfig(str(source))
    manifest = config.freeze_manifest()

    codex_projection = build_native_projection(manifest, "codex")
    projection_root = tmp_path / "projection"
    receipt = materialize_native_projection(
        config,
        manifest,
        codex_projection,
        actor="agent:owner:node:codex",
        entity_token="entity-token",
        projection_root=projection_root,
    )
    native_root = tmp_path / "native"
    shutil.copytree(projection_root, native_root)
    verify_codex_native_projection(receipt, native_root)

    (native_root / "AGENTS.md").write_text("drifted")
    with pytest.raises(CodexAgentHomeError, match="AGENTS.md"):
        verify_codex_native_projection(receipt, native_root)

    claude_projection = build_native_projection(manifest, "claude")
    claude_root = tmp_path / "claude-projection"
    claude_receipt = materialize_native_projection(
        config,
        manifest,
        claude_projection,
        actor="agent:owner:node:codex",
        entity_token="entity-token",
        projection_root=claude_root,
    )
    with pytest.raises(CodexAgentHomeError, match="belongs to 'claude'"):
        verify_codex_native_projection(claude_receipt, native_root)


def test_t08_codex_consumes_p22_resolved_context_without_deriving_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "personality"
    codex_config = source / "native" / "codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text('cli_auth_credentials_store = "file"\n')
    claude_config = source / "native" / "claude" / "settings.json"
    claude_config.parent.mkdir(parents=True)
    claude_config.write_text("{}\n")
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=hyprial_home,
    )
    try:
        agent = registry.create(
            "worker",
            cwd=str(tmp_path / "work"),
            config={
                "sources": [{"path": str(source), "required": True}],
                "discovery": "explicit-only",
            },
        )
        registry.ensure_home("worker")
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="codex",
            cwd=agent.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        prepare_codex_runtime_context(context)
        assert (context.roots.native_root / "config.toml").read_text() == (
            codex_config.read_text()
        )
        sessions = context.roots.native_root / "sessions"
        assert sessions.is_symlink()
        assert sessions.resolve() == context.roots.session_root.resolve()
        thread_id = "01999999-9999-7999-8999-999999999999"
        rollout = (
            context.roots.session_root
            / "2026"
            / "09"
            / "23"
            / f"rollout-2026-09-23T18-00-00-{thread_id}.jsonl"
        )
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"session_meta"}\n')
        assert (
            codex_rollout_file_in_session_root(
                context.roots.session_root, thread_id
            )
            == rollout
        )
        assert (
            codex_rollout_target_in_session_root(
                context.roots.session_root, rollout
            )
            == rollout
        )
        mismatched_environment = context.environment()
        mismatched_environment["CODEX_HOME"] = str(tmp_path / "wrong-native")
        mismatched_launch = ChildEnvironmentLaunch(
            CompleteChildEnvironment(tuple(sorted(mismatched_environment.items()))),
            actor=context.actor,
            runtime_context=context,
            agent_home_profile_applied=True,
        )
        with pytest.raises(
            CodexAgentHomeError,
            match="disagrees with Codex runtime context",
        ):
            CodexAppServerClient(
                HarnessLaunchSpec(
                    harness="codex",
                    name="p23-codex",
                    headless=True,
                    cwd=str(tmp_path),
                ),
                command=("codex",),
                complete_launch=mismatched_launch,
            )
        # A completed real Codex 0.157 turn writes this runtime database.
        # The next launch must keep it for strict resume, not reject it as C.
        for suffix in ("", "-wal", "-shm"):
            (context.roots.native_root / f"thread_history_1.sqlite{suffix}").write_bytes(b"runtime-state")
        prepare_codex_runtime_context(context)
        # Codex 0.157 also keeps its arg0 dispatch directory between runs: a
        # .lock file and alias symlinks to its own executable. A restart (and
        # so every resume) must accept exactly that shape.
        # Review 875: the launched executable is resolved only when an arg0
        # alias exists, so a home with no aliases prepares even when codex is
        # not on the daemon's PATH.
        from hyprial.daemon.impl.harnesses.codex import native_env

        def codex_not_on_path(*_args: object, **_kwargs: object) -> Path:
            raise codex_module.CodexExecutableResolutionError(
                "Codex executable 'codex' cannot be resolved; arg0 aliases are refused"
            )

        monkeypatch.setattr(native_env, "resolve_codex_executable", codex_not_on_path)
        prepare_codex_runtime_context(context)
        arg0 = context.roots.native_root / "tmp" / "arg0" / "codex-arg0AbC123"
        arg0.mkdir(parents=True)
        (arg0 / "apply_patch").symlink_to("/usr/bin/true")
        with pytest.raises(CodexAgentHomeError, match="apply_patch -> /usr/bin/true is refused"):
            prepare_codex_runtime_context(context)
        (arg0 / "apply_patch").unlink()
        arg0.rmdir()
        monkeypatch.undo()
        arg0.mkdir(parents=True)
        (arg0 / ".lock").write_bytes(b"")
        install = tmp_path / "install" / "bin"
        install.mkdir(parents=True)
        codex_binary = install / "codex-platform"
        codex_binary.write_text("#!/bin/sh\n")
        codex_binary.chmod(0o755)
        codex_shim = install / "codex"
        codex_shim.symlink_to(codex_binary)
        launched_codex = resolve_codex_executable({"PATH": str(install)})
        assert launched_codex == codex_binary.resolve()
        for alias in ("apply_patch", "applypatch", "codex-execve-wrapper"):
            (arg0 / alias).symlink_to(codex_shim)
        prepare_codex_runtime_context(context, codex_executable=launched_codex)
        launch_environment = context.environment()
        launch_environment["PATH"] = str(install)
        client = CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(tmp_path),
            ),
            complete_launch=ChildEnvironmentLaunch(
                CompleteChildEnvironment(tuple(sorted(launch_environment.items()))),
                actor=context.actor,
                runtime_context=context,
                agent_home_profile_applied=True,
            ),
        )
        # Launched as given: argv[0] is codex's own arg0 dispatch input.
        assert client.command[0] == "codex"
        unresolved_environment = context.environment()
        unresolved_environment["PATH"] = str(tmp_path / "missing-bin")
        unresolved_context = replace(
            context,
            environment_items=tuple(sorted(unresolved_environment.items())),
        )
        with pytest.raises(
            CodexAgentHomeError,
            match="cannot be resolved; arg0 aliases are refused",
        ):
            prepare_codex_runtime_context(unresolved_context)
        # Mirror e2e-verifier's adversarial set. Only the three exact alias
        # names, with absolute link text resolving to the launched Codex, pass.
        other_home = tmp_path / "other-agent" / "bin"
        other_home.mkdir(parents=True)
        other_codex = other_home / "codex-other"
        other_codex.write_text("#!/bin/sh\n")
        other_codex.chmod(0o755)
        bad_aliases = (
            ("apply_patch", Path("/etc")),
            ("apply_patch", Path("/etc/passwd")),
            ("apply_patch", other_codex),
            ("apply_patch", Path("../../../../../../../../etc/passwd")),
            ("apply_patch", install / "missing-codex"),
            ("not-allow-listed", codex_shim),
            ("apply_patch", Path("/bin/sh")),
            ("apply_patch", Path(os.path.relpath(codex_shim, arg0))),
        )
        for alias_name, bad_target in bad_aliases:
            bad = arg0 / alias_name
            original = os.readlink(bad) if bad.is_symlink() else None
            bad.unlink(missing_ok=True)
            bad.symlink_to(bad_target)
            with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
                prepare_codex_runtime_context(
                    context, codex_executable=launched_codex
                )
            bad.unlink()
            if original is not None:
                bad.symlink_to(original)
        prepare_codex_runtime_context(context, codex_executable=launched_codex)
        bait = arg0 / "HOST_PERSONALITY_BAIT.md"
        bait.write_text("must not load\n")
        with pytest.raises(CodexAgentHomeError, match="unprojected personality"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)
        bait.unlink()
        (arg0 / ".lock").unlink()
        (arg0 / ".lock").symlink_to("/fake/codex")
        with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)
        (arg0 / ".lock").unlink()
        stray = context.roots.native_root / "tmp" / "stray-link"
        stray.symlink_to("/fake/codex")
        with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)
        stray.unlink()
        database = context.roots.native_root / "thread_history_1.sqlite"
        database.unlink()
        database.mkdir()
        with pytest.raises(CodexAgentHomeError, match="not a regular file"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)
        database.rmdir()
        database.symlink_to(context.roots.native_root / "state_5.sqlite")
        with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)
        database.unlink()
        unprojected = context.roots.native_root / "HOST_PERSONALITY_BAIT.md"
        unprojected.write_text("must not load\n")
        with pytest.raises(CodexAgentHomeError, match="unprojected personality"):
            prepare_codex_runtime_context(context, codex_executable=launched_codex)

        wrong = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="claude",
            cwd=agent.cwd,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert wrong is not None
        with pytest.raises(CodexAgentHomeError, match="received 'claude'"):
            prepare_codex_runtime_context(wrong)
    finally:
        registry.close()


def test_agent_home_authority_materializes_complete_codex_context(
    tmp_path: Path,
) -> None:
    source = tmp_path / "personality"
    config_file = source / "native" / "codex" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('cli_auth_credentials_store = "file"\n')
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=hyprial_home,
    )
    registry.create(
        "worker",
        cwd=str(tmp_path / "work"),
        config={
            "sources": [{"path": str(source), "required": True}],
            "discovery": "explicit-only",
        },
    )
    actor = AgentActor(
        registry,
        event_sink=lambda _event: None,
        home_runtime_preparers={"codex": prepare_codex_runtime_context},
    )
    try:
        context = actor.prepare_runtime_context(
            "worker",
            "codex",
            str(tmp_path / "work"),
        )
        assert context is not None and context.authority_prepared
        assert context.public_projection()["authorityPrepared"] is True
        assert context.public_projection()["launchToken"] == context.launch_token
        assert (context.roots.native_root / "config.toml").read_text() == (
            config_file.read_text()
        )
        sessions = context.roots.native_root / "sessions"
        assert sessions.is_symlink()
        assert sessions.resolve() == context.roots.session_root.resolve()
        assert Path(context.environment()["GIT_CONFIG_GLOBAL"]).is_file()
        prepare_codex_runtime_context(context)
        from hyprial.daemon.impl.harnesses.codex import CodexInteractiveAppServer

        install = tmp_path / "install" / "bin"
        install.mkdir(parents=True)
        codex_binary = install / "codex-platform"
        codex_binary.write_text("#!/bin/sh\n")
        codex_binary.chmod(0o755)
        (install / "codex").symlink_to(codex_binary)
        interactive_environment = context.environment()
        interactive_environment["PATH"] = str(install)
        server = CodexInteractiveAppServer(
            tmp_path / "unused.sock", cwd=tmp_path,
            env=interactive_environment, projection_root=context.roots.projection_root,
            native_root=context.roots.native_root, session_root=context.roots.session_root,
            authority_prepared=True,
        )
        assert server.command[0] == "codex"
        native_config = context.roots.native_root / "config.toml"
        native_config.unlink()
        with pytest.raises(CodexAgentHomeError, match="authority-prepared.*missing"):
            prepare_codex_runtime_context(context)
        with pytest.raises(CodexAgentHomeError, match="authority-prepared.*missing"):
            CodexInteractiveAppServer(
                tmp_path / "unused.sock", cwd=tmp_path,
                env=context.environment(), projection_root=context.roots.projection_root,
                native_root=context.roots.native_root, session_root=context.roots.session_root,
                authority_prepared=True,
            )
        assert not native_config.exists()
    finally:
        assert actor.drain(3.0).complete
        registry.close()


def test_t08_codex_home_without_p22_context_is_refused(tmp_path: Path) -> None:
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment(
            (
                ("CODEX_HOME", str(tmp_path / "self-derived")),
                ("HOME", str(tmp_path / "home")),
                ("PATH", os.environ.get("PATH", "/usr/bin:/bin")),
            )
        ),
        actor="agent:owner:node:p23-codex",
        agent_home_profile_applied=True,
    )

    with pytest.raises(CodexAgentHomeError, match="requires an AgentRuntimeContext"):
        CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(tmp_path),
            ),
            command=("/fake/codex",),
            complete_launch=launch,
        )


def test_t04_thread_start_and_resume_pin_the_approved_tool_environment(
    tmp_path: Path,
) -> None:
    config = _thread_config(
        tmp_path,
        {},
        None,
        managed_environment=True,
    )

    assert config["shell_environment_policy"] == {"inherit": "all"}
    assert config["allow_login_shell"] is False


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex binary is unavailable")
def test_t04_t05_t07_t13_real_codex_load_and_resume_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real P20/current app-server: projection, layers, skills, auth and resume."""

    codex_bin = shutil.which("codex")
    assert codex_bin is not None
    # The managed test sandbox denies the host `ps` call used only to bind a
    # birth marker.  Keep real process groups/signals and replace that single
    # read-only provenance probe; loader, RPC, auth and rollout paths remain
    # the installed Codex binary.
    monkeypatch.setattr(codex_module, "_process_birth_identity", lambda _pid: "p23")
    version = subprocess.run(
        [codex_bin, "--version"],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "version-home")},
    ).stdout.strip()
    version_match = re.fullmatch(r"codex-cli (?P<version>\d+\.\d+\.\d+)", version)
    assert version_match is not None

    workspace_parent = tmp_path / "workspace"
    workspace = workspace_parent / "repo"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "AGENTS.md").write_text("P23_CODEX_PROJECT_INSTRUCTION\n")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "config.toml").write_text(
        'model = "p23-codex-project-model"\n[features]\nmemories = false\n'
    )
    project_skill = workspace / ".agents" / "skills" / "p23-codex-project"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "---\nname: p23-codex-project\n"
        "description: P23 project sentinel.\n---\n"
    )
    outside_skill = workspace_parent / ".agents" / "skills" / "outside-bait"
    outside_skill.mkdir(parents=True)
    (outside_skill / "SKILL.md").write_text(
        "---\nname: outside-bait\ndescription: Must not load.\n---\n"
    )

    source = tmp_path / "config"
    native_source = source / "native" / "codex"
    native_source.mkdir(parents=True)
    escaped_workspace = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    (native_source / "config.toml").write_text(
        'model = "p23-codex-user-model"\n'
        'cli_auth_credentials_store = "file"\n'
        "check_for_update_on_startup = false\n"
        "allow_login_shell = false\n"
        f'\n[projects."{escaped_workspace}"]\ntrust_level = "trusted"\n'
    )
    (native_source / "AGENTS.md").write_text("P23_CODEX_USER_INSTRUCTION\n")
    user_skill = source / "shared" / "skills" / "p23-codex-user"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        "---\nname: p23-codex-user\ndescription: P23 user sentinel.\n---\n"
    )
    config = AgentConfig(str(source))
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, "codex")
    projection_root = tmp_path / "projection"
    receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor="agent:owner:node:p23-codex",
        entity_token="p23-incarnation",
        projection_root=projection_root,
    )
    native_root = tmp_path / "agent" / "secrets" / "native" / "codex"
    native_root.mkdir(parents=True, mode=0o700)
    session_root = tmp_path / "agent" / "state" / "sessions" / "codex"
    session_root.mkdir(parents=True, mode=0o700)
    shutil.copy2(
        Path(__file__).resolve().parents[6] / 'tests/fixtures/agent_home_p20/codex/native-root/auth.json',
        native_root / "auth.json",
    )
    (native_root / "auth.json").chmod(0o600)

    fake_host_home = tmp_path / "host-home"
    (fake_host_home / ".codex").mkdir(parents=True)
    (fake_host_home / ".codex" / "AGENTS.md").write_text("HOST_BAIT\n")
    isolated_home = tmp_path / "agent" / "state" / "home"
    temporary = tmp_path / "tmp"
    isolated_home.mkdir(parents=True)
    temporary.mkdir()
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(isolated_home),
        "TMPDIR": str(temporary),
        "TERM": "xterm-256color",
        "CODEX_HOME": str(native_root),
    }
    context = _runtime_context(
        actor="agent:owner:node:p23-codex",
        source=source,
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        manifest=manifest,
        projection=projection,
        receipt=receipt,
        environment={
            key: value
            for key, value in environment.items()
            if key in {"CODEX_HOME", "HOME", "TMPDIR"}
        },
    )
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment(tuple(sorted(environment.items()))),
        actor="agent:owner:node:p23-codex",
        runtime_context=context,
    )
    version_logger = Logger.worker(
        tmp_path / "version-state", runtime="codex", name="p23-codex-version"
    )

    async def exercise() -> None:
        first = CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(workspace),
            ),
            command=(codex_bin,),
            complete_launch=launch,
            logger=version_logger,
        )
        async with first:
            thread_id = first.session_ref
            evidence = first.native_load_evidence
            assert thread_id is not None
            assert evidence is not None
            assert evidence.layer_types == ("project", "user", "system")
            assert evidence.effective_model == "p23-codex-project-model"
            assert evidence.account_type == "apiKey"
            assert evidence.user_skills == ("p23-codex-user",)
            assert evidence.project_skills == ("p23-codex-project",)
        resumed = CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(workspace),
                session_ref=thread_id,
            ),
            session_ref=thread_id,
            command=(codex_bin,),
            complete_launch=launch,
        )
        with pytest.raises(CodexAgentHomeError, match="resume target.*not found"):
            async with resumed:
                pass
        missing = CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(workspace),
                session_ref="01999999-9999-7999-8999-999999999999",
            ),
            session_ref="01999999-9999-7999-8999-999999999999",
            command=(codex_bin,),
            complete_launch=launch,
        )
        with pytest.raises(CodexAgentHomeError, match="resume target.*not found"):
            async with missing:
                pass
        # A thread with no turn is retained by the real 0.155.x app-server in
        # its native SQLite state, so restart/resume is real while no rollout
        # JSONL exists yet.  The transfer locator must fail loudly instead of
        # pretending that an absent transcript is transferable.  A real turn
        # (and therefore a real service subject) remains a separately reported
        # NOT_RUN cell.
        with pytest.raises(SessionFileNotFound):
            codex_rollout_file_in_session_root(session_root, thread_id)
        assert (native_root / "state_5.sqlite").is_file()
        with pytest.raises(SessionFileNotFound):
            codex_rollout_file_in_session_root(
                fake_host_home / ".codex" / "sessions", thread_id
            )

        version_warnings = [
            json.loads(line)
            for line in version_logger.path.read_text(encoding="utf-8").splitlines()
            if '"event":"codex.version.unrecorded"' in line
        ]
        installed = version_match.group("version")
        recorded = {
            row.key.version
            for row in load_packaged_support_matrix().rows
            if row.key.harness == "codex"
        }
        # A warn only for a version the support matrix has not recorded.
        expected = [] if installed in recorded else [installed]
        assert [warning["version"] for warning in version_warnings] == expected

    asyncio.run(exercise())

    socket_root = Path(
        tempfile.mkdtemp(prefix="p23-codex-tui-", dir="/private/tmp")
    )
    socket_path = socket_root / "app.sock"
    server = CodexInteractiveAppServer(
        socket_path,
        cwd=workspace,
        command=(codex_bin,),
        env=environment,
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
    )
    master_fd, slave_fd = pty.openpty()
    tui: subprocess.Popen[bytes] | None = None
    try:
        server.start()
        interactive_evidence = server.native_load_evidence
        assert interactive_evidence is not None
        assert interactive_evidence.layer_types == (
            "sessionFlags",
            "project",
            "user",
            "system",
        )
        assert interactive_evidence.effective_model == "p23-codex-project-model"
        assert interactive_evidence.account_type == "apiKey"
        tui = subprocess.Popen(
            [
                codex_bin,
                "--no-alt-screen",
                "--remote",
                f"unix://{socket_path}",
            ],
            cwd=workspace,
            env=environment,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + 15.0
        discovered: str | None = None
        while time.monotonic() < deadline:
            assert tui.poll() is None, "real Codex TUI exited before thread discovery"
            try:
                discovered, _mode = server.discover_thread()
            except (ConnectionError, codex_module.CodexAppServerRpcError):
                time.sleep(0.05)
                continue
            break
        assert discovered is not None
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        if tui is not None and tui.poll() is None:
            os.killpg(tui.pid, signal.SIGTERM)
            try:
                tui.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                os.killpg(tui.pid, signal.SIGKILL)
                tui.wait(timeout=3.0)
        os.close(master_fd)
        server.stop()
        shutil.rmtree(socket_root)
    verify_codex_native_projection(receipt, native_root)


@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex binary is unavailable")
def test_t07_real_codex_ephemeral_custom_provider_needs_no_openai_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_bin = shutil.which("codex")
    assert codex_bin is not None
    monkeypatch.setattr(codex_module, "_process_birth_identity", lambda _pid: "p23")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)

    escaped_workspace = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    source = tmp_path / "config"
    native_source = source / "native" / "codex"
    native_source.mkdir(parents=True)
    (native_source / "config.toml").write_text(
        'cli_auth_credentials_store = "ephemeral"\n'
        "check_for_update_on_startup = false\n"
        f'\n[projects."{escaped_workspace}"]\ntrust_level = "trusted"\n'
    )
    config = AgentConfig(str(source))
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, "codex")
    projection_root = tmp_path / "projection"
    receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor="agent:owner:node:p23-codex",
        entity_token="p23-incarnation",
        projection_root=projection_root,
    )
    native_root = tmp_path / "agent" / "secrets" / "native" / "codex"
    native_root.mkdir(parents=True, mode=0o700)
    session_root = tmp_path / "agent" / "state" / "sessions" / "codex"
    session_root.mkdir(parents=True, mode=0o700)
    isolated_home = tmp_path / "agent" / "state" / "home"
    temporary = tmp_path / "tmp"
    isolated_home.mkdir()
    temporary.mkdir()
    sentinel = "P23_LOCAL_PROVIDER_NOT_A_REAL_KEY"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(isolated_home),
        "TMPDIR": str(temporary),
        "CODEX_HOME": str(native_root),
        "DEEPSEEK_API_KEY": sentinel,
    }
    context = _runtime_context(
        actor="agent:owner:node:p23-codex",
        source=source,
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        manifest=manifest,
        projection=projection,
        receipt=receipt,
        environment={
            key: value
            for key, value in environment.items()
            if key in {"CODEX_HOME", "HOME", "TMPDIR"}
        },
    )
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment(tuple(sorted(environment.items()))),
        actor="agent:owner:node:p23-codex",
        runtime_context=context,
    )

    async def exercise() -> None:
        client = CodexAppServerClient(
            HarnessLaunchSpec(
                harness="codex",
                name="p23-codex",
                headless=True,
                cwd=str(workspace),
                model_provider="deepseek",
                model="deepseek-flash",
            ),
            command=(codex_bin,),
            complete_launch=launch,
        )
        assert sentinel not in client.command
        async with client:
            evidence = client.native_load_evidence
            assert evidence is not None
            assert evidence.auth_store == "ephemeral"
            assert evidence.account_type is None
            assert evidence.requires_openai_auth is False

    asyncio.run(exercise())
