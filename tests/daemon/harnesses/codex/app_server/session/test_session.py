from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses.codex import THREAD_START_TIMEOUT_SECONDS_DEFAULT, _git_metadata_roots, _spawn_managed_codex, CodexAppServerProcess, _OwnedProcessGroup

from ..support import (
    FAKE_CODEX,
    _git_repo,
    _prompt,
    _wire_worker_channel,
    delivery,
    fake_process,
    read_log,
    spec,
    wait_for_results,
)


def test_posix_codex_spawn_preserves_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    sentinel = object()

    async def spawn(*command: str, **options: object) -> object:
        observed["command"] = command
        observed["options"] = options
        return sentinel

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.asyncio.create_subprocess_exec", spawn)

    result = asyncio.run(
        _spawn_managed_codex(
            ("codex", "app-server"),
            _OwnedProcessGroup(),
            cwd="/work",
        )
    )

    assert result is sentinel
    assert observed == {
        "command": ("codex", "app-server"),
        "options": {"cwd": "/work", "start_new_session": True},
    }


def test_mid_turn_crash_resumes_thread_and_retries_head_delivery(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    marker = tmp_path / "crash-marker"
    process = fake_process("crash-once", log_path=log_path, state_path=marker)
    try:
        assert process.enqueue(delivery("delivery-1", "survive this"))
        [result] = wait_for_results(process, 1, timeout=10.0)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("survive this done")
        methods = [message.get("method") for message in read_log(log_path)]
        assert methods.count("thread/start") == 1
        assert methods.count("thread/resume") >= 1
    finally:
        process.stop()


def test_resume_accepts_a_thread_already_loaded_without_a_rollout() -> None:
    process = CodexAppServerProcess(
        spec(session_ref="codex-thread-1"),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_MODE": "loaded-fallback"},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.session_ref == "codex-thread-1"
    finally:
        process.stop()


def test_worker_channel_config_rides_thread_resume_on_reconnect(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    channel = _wire_worker_channel(tmp_path)
    process = CodexAppServerProcess(
        spec(session_ref="codex-thread-1"),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
        worker_channel=channel,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        resume = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/resume"
        )
        server = resume["params"]["config"]["mcp_servers"]["harness-bridge"]
        assert server["command"] == channel.mcp_server["command"]
        # ⚠️ The reconnect path needs it too. If pre-approval rode `thread/start`
        # only, a resumed session would quietly lose it and the harness tools
        # would start being refused again mid-conversation -- which is the exact
        # shape of the failure this change exists to fix, returning by a door
        # nobody was watching.
        assert server["default_tools_approval_mode"] == "auto"
    finally:
        process.stop()


def test_git_metadata_roots_plain_clone(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "clone")
    assert _git_metadata_roots(repo) == [str((repo / ".git").resolve())]


def test_git_metadata_roots_linked_worktree_covers_git_dir_and_common_dir(
    tmp_path: Path,
) -> None:
    main = _git_repo(tmp_path / "main")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "-b", "probe"],
        check=True,
    )
    # The linked worktree's `.git` is a FILE; the real metadata is elsewhere.
    assert (linked / ".git").is_file()
    roots = _git_metadata_roots(linked)
    git_dir = (main / ".git" / "worktrees" / "linked").resolve()
    common_dir = (main / ".git").resolve()
    assert str(git_dir) in roots
    assert str(common_dir) in roots
    assert roots == [str(git_dir), str(common_dir)]


def test_git_metadata_roots_empty_cwd_grants_future_git_directory(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()
    assert _git_metadata_roots(cwd) == [str(cwd / ".git")]


def test_git_metadata_roots_empty_symlink_cwd_grants_given_and_resolved_paths(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    cwd = tmp_path / "linked-cwd"
    cwd.symlink_to(target, target_is_directory=True)

    assert _git_metadata_roots(cwd) == [
        str(cwd / ".git"),
        str((target / ".git").resolve()),
    ]


def test_git_metadata_roots_subdirectory_is_not_a_worktree_root(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "clone")
    nested = repo / "src"
    nested.mkdir()
    # cwd below the root already works (layout F); do not grant anything.
    assert _git_metadata_roots(nested) == []


def test_thread_resume_regrants_git_write_on_reconnect(tmp_path: Path) -> None:
    """The reconnect path must not silently lose the grant."""

    log_path = tmp_path / "wire.jsonl"
    repo = _git_repo(tmp_path / "clone")
    process = CodexAppServerProcess(
        spec(
            args=("-a", "on-request", "-s", "workspace-write"),
            session_ref="codex-thread-1",
            cwd=str(repo),
        ),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        resume = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/resume"
        )
        roots = resume["params"]["config"]["sandbox_workspace_write"][
            "writable_roots"
        ]
        assert roots == [str((repo / ".git").resolve())]
    finally:
        process.stop()


def test_thread_start_default_budget_covers_the_measured_session_start() -> None:
    # 9-43 s measured on hq (2026-09-05/06, three samples per run); the budget
    # must clear the slowest sample with margin, not the fastest.
    assert THREAD_START_TIMEOUT_SECONDS_DEFAULT >= 60.0
