"""Fresh Codex agent homes validate without a projected user config file."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex import (
    CodexAgentHomeError,
    CodexInteractiveAppServer,
    _validate_codex_native_load,
    prepare_codex_runtime_context,
)
from hyprial.daemon.impl.harnesses.codex.launch.inputs import (
    build_codex_launch_inputs,
)
from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.runtime import (
    resolve_agent_runtime_context,
)
from hyprial.kernel import HarnessLaunchSpec

from ..support import _native_responses
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


@pytest.mark.parametrize("padding", ["", "x" * 10000, "中" * 3000], ids=["short", "bounded", "utf8"])
def test_skill_loader_error_preserves_cause_but_redacts_secrets(tmp_path, padding):
    root, cwd, initialize, config, account, skills = _native_responses(tmp_path)
    skills["data"][0]["skills"] = [{
        "name": "healthy", "path": str(root / "skills/healthy/SKILL.md"), "scope": "user",
    }]
    skills["data"][0]["errors"] = [{
        "path": str(root / "skills/squire/SKILL.md"),
        "message": "missing YAML frontmatter delimited by --- "
                   "api_key=PRIVATE_KEY_SENTINEL Authorization: Bearer PRIVATE_BEARER\n"
                   "https://example.invalid/?token=PRIVATE_URL "
                   "sk-proj-PRIVATE_BARE_KEY_SENTINEL "
                   "abcdefghPRIVATE.abcdefghPRIVATE.abcdefghPRIVATE " + padding,
        "token": "PRIVATE_STRUCTURED_TOKEN",
    }]
    evidence = _validate_codex_native_load(
        initialize=initialize, config_read=config, account_read=account,
        skills_list=skills, native_root=root, cwd=cwd, model_provider="openai",
    )
    assert evidence.user_skills == ("healthy",)
    assert len(evidence.warnings) == 1
    message = evidence.warnings[0]
    assert "missing YAML frontmatter delimited by ---" in message
    assert "skills/squire/SKILL.md" in message
    assert "PRIVATE_" not in message
    assert len(message.encode()) <= 4096


@pytest.mark.parametrize("errors", ["invalid", [{"path": "/skill"}], ["invalid"]])
def test_malformed_loader_errors_still_fail(tmp_path, errors):
    root, cwd, initialize, config, account, skills = _native_responses(tmp_path)
    skills["data"][0]["errors"] = errors
    with pytest.raises(CodexAgentHomeError, match="malformed loader errors"):
        _validate_codex_native_load(
            initialize=initialize, config_read=config, account_read=account,
            skills_list=skills, native_root=root, cwd=cwd, model_provider="openai",
        )


def _fresh_responses(tmp_path: Path) -> tuple[object, ...]:
    responses = list(_native_responses(tmp_path))
    native_root = responses[0]
    config_read = responses[3]
    assert isinstance(native_root, Path)
    assert isinstance(config_read, dict)
    (native_root / "config.toml").unlink()
    user_layer = next(
        layer for layer in config_read["layers"] if layer["name"]["type"] == "user"
    )
    user_layer["config"] = {}
    return tuple(responses)


def test_native_loader_accepts_fresh_default_without_user_config(
    tmp_path: Path,
) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _fresh_responses(tmp_path)
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
    assert not (native_root / "config.toml").exists()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="real Codex binary is unavailable",
)
def test_real_codex_validates_fresh_daemon_agent_home(
    tmp_path: Path,
    provider: str,
) -> None:
    """Exercise the daemon's complete fresh-agent launch-validation path."""

    codex_bin = shutil.which("codex")
    assert codex_bin is not None
    home = tmp_path / "home"
    hyprial_home = tmp_path / "hyprial"
    state_dir = tmp_path / "state"
    source = tmp_path / "agent-config"
    workspace = tmp_path / "workspace"
    temporary = tmp_path / "tmp"
    for path in (home, hyprial_home, state_dir, source, workspace, temporary):
        path.mkdir(parents=True)
    (workspace / ".git").mkdir()
    registry = AgentRegistry(
        state_dir / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=hyprial_home,
    )
    socket_root = Path(
        tempfile.mkdtemp(
            prefix="codex-fresh-",
            dir="/private/tmp" if Path("/private/tmp").is_dir() else "/tmp",
        )
    )
    server: CodexInteractiveAppServer | None = None
    try:
        agent = registry.create(
            "worker",
            cwd=str(workspace),
            config=AgentConfig(str(source)).to_json(),
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
        assert not (context.roots.native_root / "config.toml").exists()

        environment = context.environment()
        environment.update(
            {
                "HOME": str(home),
                "PATH": os.environ["PATH"],
                "TMPDIR": str(temporary),
            }
        )
        model = None
        if provider == "openai":
            auth = context.roots.native_root / "auth.json"
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "apikey",
                        "OPENAI_API_KEY": "not-a-real-provider-key",
                    }
                ),
                encoding="utf-8",
            )
            auth.chmod(0o600)
        else:
            model = "deepseek-flash"
            environment["DEEPSEEK_API_KEY"] = "not-a-real-provider-key"

        inputs = build_codex_launch_inputs(
            HarnessLaunchSpec(
                "codex",
                "codex-tui",
                False,
                cwd=str(workspace),
                model_provider=provider,
                model=model,
            ),
            environment,
            tool_profile=True,
            interactive_channel={
                "actor": context.actor,
                "session_ref_file": state_dir / "session-ref",
                "hyprial_home": hyprial_home,
                "state_dir": state_dir,
            },
            allow_legacy_home_fallback=False,
        )
        server = CodexInteractiveAppServer(
            socket_root / "app.sock",
            cwd=workspace,
            command=(codex_bin,),
            env={**environment, **inputs.environment},
            config_args=inputs.argv,
            expected_session_flags=inputs.expected_session_flags,
            model_provider=provider,
            projection_root=context.roots.projection_root,
            native_root=context.roots.native_root,
            session_root=context.roots.session_root,
        )
        server.start()

        evidence = server.native_load_evidence
        assert evidence is not None
        assert evidence.codex_home == str(context.roots.native_root)
        assert evidence.auth_store == (
            "file" if provider == "openai" else "ephemeral"
        )
    finally:
        if server is not None:
            server.stop()
        registry.close()
        shutil.rmtree(socket_root)


def test_native_loader_rejects_missing_user_config_with_nonempty_layer(
    tmp_path: Path,
) -> None:
    native_root, workspace, initialize, config_read, account_read, skills = (
        _fresh_responses(tmp_path)
    )
    user_layer = next(
        layer for layer in config_read["layers"] if layer["name"]["type"] == "user"
    )
    user_layer["config"]["model"] = "untrusted"

    with pytest.raises(CodexAgentHomeError, match="non-empty user layer"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native_root,
            cwd=workspace,
            model_provider="openai",
        )
