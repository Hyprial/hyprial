"""Tests for atomic OAuth token projection and cleanup in agy native_env."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.harnesses.agy.native_env import (
    AGY_CLI_STATE_DIRNAME,
    AGY_OAUTH_TOKEN_FILENAME,
    AgyAgentHomeError,
    prepare_agy_runtime_context,
    sync_agy_oauth_token,
)
from hyprial.identity.impl.agents.runtime import AgentRuntimeRoots


def _make_context(root: Path, *, actor: str = "agent:op:node-1:agy-w1", harness: str = "agy") -> SimpleNamespace:
    agent_home = root / actor.replace(":", "_")
    projection_root = agent_home / "state" / "config" / "tok" / "rev1" / "native" / harness
    native_root = agent_home / "secrets" / "native" / harness
    session_root = agent_home / "state" / "sessions" / harness
    tool_home = agent_home / "state" / "home"
    for directory in (
        projection_root,
        native_root,
        session_root,
        tool_home,
        agent_home / "secrets" / "tools" / "xdg",
        agent_home / "state" / "xdg" / "cache",
        agent_home / "state" / "xdg" / "data",
        agent_home / "state" / "xdg" / "state",
        agent_home / "state" / "config" / "tools",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    roots = AgentRuntimeRoots(
        agent_home=agent_home,
        config_source=agent_home / "config.yaml",
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        tool_home=tool_home,
        xdg_config_home=agent_home / "secrets" / "tools" / "xdg",
        xdg_cache_home=agent_home / "state" / "xdg" / "cache",
        xdg_data_home=agent_home / "state" / "xdg" / "data",
        xdg_state_home=agent_home / "state" / "xdg" / "state",
        tool_profile_root=agent_home / "state" / "config" / "tools",
    )
    return SimpleNamespace(
        actor=actor,
        harness=harness,
        roots=roots,
        shared_credential=None,
    )


def test_prepare_copies_token_atomically_and_skips_when_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_context(tmp_path)
    src = ctx.roots.native_root / "antigravity-oauth-token"
    src.write_bytes(b'{"access_token":"tok-1"}\n')
    dst = ctx.roots.session_root / "antigravity-cli" / "antigravity-oauth-token"

    prepare_agy_runtime_context(ctx)

    assert dst.is_file()
    assert not dst.is_symlink()
    assert dst.read_bytes() == b'{"access_token":"tok-1"}\n'

    def fail_replace(_src: object, _dst: object) -> None:
        raise AssertionError("os.replace should not run when bytes are unchanged")

    monkeypatch.setattr(os, "replace", fail_replace)
    status = sync_agy_oauth_token(
        native_root=ctx.roots.native_root,
        session_root=ctx.roots.session_root,
    )
    assert status == "unchanged"


def test_logout_removes_session_copy_on_next_prepare(tmp_path: Path) -> None:
    ctx = _make_context(tmp_path)
    src = ctx.roots.native_root / "antigravity-oauth-token"
    src.write_bytes(b"token-v1")
    dst = ctx.roots.session_root / "antigravity-cli" / "antigravity-oauth-token"

    prepare_agy_runtime_context(ctx)
    assert dst.is_file()

    src.unlink()
    prepare_agy_runtime_context(ctx)
    assert not dst.exists()


def test_two_workers_keep_isolated_session_copies(tmp_path: Path) -> None:
    ctx_a = _make_context(tmp_path, actor="agent:op:node-1:agy-a")
    ctx_b = _make_context(tmp_path, actor="agent:op:node-1:agy-b")
    (ctx_a.roots.native_root / "antigravity-oauth-token").write_bytes(b"token-a")
    (ctx_b.roots.native_root / "antigravity-oauth-token").write_bytes(b"token-b")

    prepare_agy_runtime_context(ctx_a)
    prepare_agy_runtime_context(ctx_b)

    dst_a = ctx_a.roots.session_root / "antigravity-cli" / "antigravity-oauth-token"
    dst_b = ctx_b.roots.session_root / "antigravity-cli" / "antigravity-oauth-token"
    assert dst_a.read_bytes() == b"token-a"
    assert dst_b.read_bytes() == b"token-b"

    (ctx_a.roots.native_root / "antigravity-oauth-token").unlink()
    prepare_agy_runtime_context(ctx_a)
    assert not dst_a.exists()
    assert dst_b.read_bytes() == b"token-b"


def test_rejects_non_agy_context_and_symlink_source(tmp_path: Path) -> None:
    bad = _make_context(tmp_path, harness="codex")
    with pytest.raises(AgyAgentHomeError):
        prepare_agy_runtime_context(bad)

    ctx = _make_context(tmp_path, actor="agent:op:node-1:agy-sym")
    real = tmp_path / "outside-token"
    real.write_bytes(b"secret")
    (ctx.roots.native_root / "antigravity-oauth-token").symlink_to(real)
    with pytest.raises(AgyAgentHomeError):
        prepare_agy_runtime_context(ctx)


@pytest.mark.parametrize("path_name", ["session", "cli", "token"])
def test_rejects_symlinked_projection_paths(tmp_path: Path, path_name: str) -> None:
    ctx = _make_context(tmp_path, actor=f"agent:op:node-1:agy-{path_name}")
    outside = tmp_path / f"outside-{path_name}"
    outside.mkdir()
    if path_name == "session":
        session_root = ctx.roots.session_root
        session_root.rmdir()
        session_root.symlink_to(outside, target_is_directory=True)
    elif path_name == "cli":
        cli_dir = ctx.roots.session_root / AGY_CLI_STATE_DIRNAME
        cli_dir.symlink_to(outside, target_is_directory=True)
    else:
        cli_dir = ctx.roots.session_root / AGY_CLI_STATE_DIRNAME
        cli_dir.mkdir()
        token = ctx.roots.session_root / AGY_CLI_STATE_DIRNAME / AGY_OAUTH_TOKEN_FILENAME
        token.symlink_to(outside / "token")
    with pytest.raises(AgyAgentHomeError):
        sync_agy_oauth_token(native_root=ctx.roots.native_root, session_root=ctx.roots.session_root)
