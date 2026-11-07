from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch
from hyprial.identity.impl.agents.runtime import context as context_module
from hyprial.identity.impl.agents.runtime.context import (
    AgentRuntimeError,
    resolve_agent_runtime_context,
    shared_credential_status,
    validate_shared_credential_environment,
)
from hyprial.daemon.impl.harnesses.codex.native_env import prepare_codex_runtime_context
from hyprial.daemon.impl.harnesses.claude.runtime import ClaudeRuntimeError
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def _configured_registry(
    tmp_path: Path,
    *,
    harness: str,
    target: Path,
) -> tuple[AgentRegistry, Path, Path]:
    personality = tmp_path / f"{harness}-personality"
    native_source = personality / "native" / harness
    native_source.mkdir(parents=True)
    if harness == "codex":
        (native_source / "config.toml").write_text(
            'cli_auth_credentials_store = "file"\n', encoding="utf-8"
        )
    elif harness == "claude":
        (native_source / "settings.json").write_text("{}\n", encoding="utf-8")
    else:
        (native_source / "settings.json").write_text("{}\n", encoding="utf-8")

    work = tmp_path / f"{harness}-work"
    work.mkdir()
    home = tmp_path / f"{harness}-hyprial"
    home.mkdir()
    registry = AgentRegistry(
        tmp_path / f"{harness}-agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=home,
    )
    try:
        registry.create(
            "worker",
            cwd=str(work),
            config={
                "sources": [{"path": str(personality), "required": True}],
                "discovery": "explicit-only",
                "sharedCredentials": {harness: str(target)},
            },
        )
        receipt = registry.ensure_home("worker")
    except BaseException:
        registry.close()
        raise
    credential_name = ".credentials.json" if harness == "claude" else "auth.json"
    credential = Path(receipt.path) / "secrets" / "native" / harness / credential_name
    credential.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential.parent.parent.parent.chmod(0o700)
    (credential.parent.parent).chmod(0o700)
    credential.parent.chmod(0o700)
    return registry, work, credential


def _write_target(path: Path, *, mode: int = 0o600) -> None:
    path.write_text("SYNTHETIC_SHARED_AUTH_NOT_A_SECRET\n", encoding="utf-8")
    path.chmod(mode)


@pytest.mark.parametrize("harness", ["codex", "pi"])
def test_p2_start_accepts_only_the_configured_shared_credential_link(
    tmp_path: Path, harness: str
) -> None:
    target = tmp_path / f"{harness}-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness=harness, target=target
    )
    credential.symlink_to(target)
    try:
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness=harness,
            cwd=str(work),
            tool_profile=TEST_TOOL_PROFILE,
        )
    finally:
        registry.close()

    assert context is not None
    assert context.auth_method == "native-shared-link"


@pytest.mark.parametrize("harness", ["codex", "pi"])
def test_p2_start_rejects_a_shared_link_to_a_non_designated_target(
    tmp_path: Path, harness: str
) -> None:
    target = tmp_path / f"{harness}-designated-auth"
    other = tmp_path / f"{harness}-other-auth"
    _write_target(target)
    _write_target(other)
    registry, work, credential = _configured_registry(
        tmp_path, harness=harness, target=target
    )
    credential.symlink_to(other)
    try:
        with pytest.raises(AgentRuntimeError) as raised:
            resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness=harness,
                cwd=str(work),
                tool_profile=TEST_TOOL_PROFILE,
            )
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_DIVERGED"
    assert "worker" in str(raised.value)
    assert str(credential) in str(raised.value)


@pytest.mark.parametrize("harness", ["codex", "pi"])
@pytest.mark.parametrize("condition", ["wrong-mode", "dangling", "regular-copy"])
def test_p2_start_rejects_an_invalid_or_diverged_shared_credential(
    tmp_path: Path, harness: str, condition: str
) -> None:
    target = tmp_path / f"{harness}-shared-auth"
    _write_target(target, mode=0o644 if condition == "wrong-mode" else 0o600)
    registry, work, credential = _configured_registry(
        tmp_path, harness=harness, target=target
    )
    if condition == "regular-copy":
        credential.write_text("SYNTHETIC_DIVERGED_COPY\n", encoding="utf-8")
        credential.chmod(0o600)
    else:
        credential.symlink_to(target)
        if condition == "dangling":
            target.unlink()
    try:
        with pytest.raises(AgentRuntimeError) as raised:
            resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness=harness,
                cwd=str(work),
                tool_profile=TEST_TOOL_PROFILE,
            )
    finally:
        registry.close()

    expected = (
        "SHARED_CREDENTIAL_DIVERGED"
        if condition == "regular-copy"
        else "SHARED_CREDENTIAL_INVALID"
    )
    assert raised.value.code == expected


def test_p2_start_rejects_a_shared_target_not_owned_by_the_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "codex-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="codex", target=target
    )
    credential.symlink_to(target)
    real_violation = context_module.private_metadata_violation

    def _foreign_target(path: Path, metadata, *, mode: int | None):
        if Path(path) == target:
            return "wrong-owner"
        return real_violation(path, metadata, mode=mode)

    monkeypatch.setattr(
        context_module, "private_metadata_violation", _foreign_target
    )
    try:
        with pytest.raises(AgentRuntimeError) as raised:
            resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness="codex",
                cwd=str(work),
                tool_profile=TEST_TOOL_PROFILE,
            )
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_INVALID"


def test_p2_start_rejects_a_shared_target_inside_the_agent_cwd(
    tmp_path: Path,
) -> None:
    target = tmp_path / "codex-work" / "shared-auth"
    registry, work, credential = _configured_registry(
        tmp_path, harness="codex", target=target
    )
    _write_target(target)
    credential.symlink_to(target)
    try:
        with pytest.raises(AgentRuntimeError) as raised:
            resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness="codex",
                cwd=str(work),
                tool_profile=TEST_TOOL_PROFILE,
            )
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_INVALID"
    assert "outside the agent cwd" in str(raised.value)


def test_p2_claude_shared_file_link_is_refused_on_macos(
    tmp_path: Path,
) -> None:
    target = tmp_path / "claude-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="claude", target=target
    )
    credential.symlink_to(target)
    try:
        with pytest.raises((AgentRuntimeError, ClaudeRuntimeError)) as raised:
            resolve_agent_runtime_context(
                registry=registry,
                agent_name="worker",
                harness="claude",
                cwd=str(work),
                tool_profile=TEST_TOOL_PROFILE,
            )
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_UNSUPPORTED"
    assert "Keychain" in str(raised.value)


def test_codex_rechecks_divergence_at_the_final_start_boundary(
    tmp_path: Path,
) -> None:
    target = tmp_path / "codex-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="codex", target=target
    )
    credential.symlink_to(target)
    try:
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="codex",
            cwd=str(work),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        credential.unlink()
        credential.write_text("SYNTHETIC_DIVERGED_COPY\n", encoding="utf-8")
        credential.chmod(0o600)
        with pytest.raises(AgentRuntimeError) as raised:
            prepare_codex_runtime_context(context)
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_DIVERGED"


@pytest.mark.parametrize(
    ("runtime_name", "runtime_value"),
    (
        ("GIT_CONFIG_GLOBAL", "/ordinary/runtime/gitconfig"),
        ("XDG_CONFIG_HOME", "/ordinary/runtime/xdg"),
    ),
)
def test_shared_link_accepts_ordinary_runtime_environment_without_a_secret_grant(
    tmp_path: Path,
    runtime_name: str,
    runtime_value: str,
) -> None:
    """Only grant-declared credential fields can conflict with a shared link."""

    target = tmp_path / "codex-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="codex", target=target
    )
    credential.symlink_to(target)
    try:
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="codex",
            cwd=str(work),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        hyprial_home = tmp_path / "codex-hyprial"
        channel = build_worker_channel(
            actor=registry.require("worker").uri,
            session_ref="shared-ordinary-runtime",
            hyprial_home=hyprial_home,
            state_dir=tmp_path / "state",
            runtime_context=context,
        )

        launch = compose_worker_child_launch(
            registry=registry,
            hyprial_home=hyprial_home,
            channel=channel,
            environ={"PATH": "/usr/bin:/bin", runtime_name: runtime_value},
            agent_name="worker",
        )
    finally:
        registry.close()

    assert runtime_name in launch.environment.for_exec()


@pytest.mark.parametrize("environment_name", ("OPENAI_API_KEY", "FORGEJO_TOKEN"))
def test_shared_link_and_explicit_environment_credential_cannot_coexist(
    tmp_path: Path,
    environment_name: str,
) -> None:
    target = tmp_path / "codex-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="codex", target=target
    )
    credential.symlink_to(target)
    try:
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="worker",
            harness="codex",
            cwd=str(work),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        with pytest.raises(AgentRuntimeError) as raised:
            validate_shared_credential_environment(
                context, {environment_name: "SYNTHETIC_NOT_A_SECRET"}
            )
    finally:
        registry.close()

    assert raised.value.code == "SHARED_CREDENTIAL_CONFLICT"


def test_agent_status_reports_ready_warning_and_divergence(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pi-shared-auth"
    _write_target(target)
    registry, work, credential = _configured_registry(
        tmp_path, harness="pi", target=target
    )
    credential.symlink_to(target)
    try:
        agent = registry.require("worker")
        ready = shared_credential_status(registry=registry, agent=agent)
        assert ready == [
            {
                "harness": "pi",
                "authMode": "native-shared-link",
                "nativePath": str(credential),
                "targetPath": str(target),
                "status": "warning",
                "warning": (
                    "Pi 0.85.1 locks the per-agent link path, so concurrent "
                    "refreshes are not mutually excluded"
                ),
            }
        ]

        credential.unlink()
        credential.write_text("SYNTHETIC_DIVERGED_COPY\n", encoding="utf-8")
        credential.chmod(0o600)
        diverged = shared_credential_status(registry=registry, agent=agent)
    finally:
        registry.close()

    assert diverged[0]["status"] == "error"
    assert diverged[0]["code"] == "SHARED_CREDENTIAL_DIVERGED"
