"""Rule-syntax and workspace coverage for the read-only readiness preflight."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.identity.impl.agents.home.readiness import (
    BLOCKED,
    PASS,
    UNKNOWN,
)

from .test_readiness import (
    _assess,
    _checks,
    _config,
    _detail,
    _home,
    _install_tools,
    _settings,
    _workspace,
)


def _local_settings(
    path: Path,
    allow: list[str],
    *,
    deny: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions: dict[str, Any] = {"allow": allow}
    if deny:
        permissions["deny"] = deny
    path.write_text(json.dumps({"permissions": permissions}), encoding="utf-8")
    return path


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create the standard main checkout + linked worktree marker layout."""

    main = tmp_path / "main-checkout"
    gitdir = main / ".git" / "worktrees" / "task"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "task"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return main, worktree


def test_exact_and_glob_rules_do_not_become_broad_tool_grants(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")

    _settings(config_dir, ["Bash(git)", "Bash(fj)"])
    exact = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(exact)["permissions:git"] == PASS
    assert _checks(exact)["permissions:fj"] == PASS
    assert exact.ready is True

    _settings(config_dir, ["Bash(git status)", "Bash(fj whoami)"])
    specific = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(specific)["permissions:git"] == UNKNOWN
    assert _checks(specific)["permissions:fj"] == UNKNOWN
    assert "only a specific invocation" in _detail(specific, "permissions:fj")
    assert "Bash(fj whoami)" in _detail(specific, "permissions:fj")
    assert specific.ready is False

    _settings(config_dir, ["Bash(git*)", "Bash(fj*)"])
    glob = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(glob)["permissions:git"] == PASS
    assert _checks(glob)["permissions:fj"] == PASS

    _settings(config_dir, ["Bash(git *)", "Bash(fj *)"])
    args_only = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(args_only)["permissions:git"] == UNKNOWN
    assert _checks(args_only)["permissions:fj"] == UNKNOWN
    assert "readiness does not infer an unspecified task command" in _detail(
        args_only, "permissions:fj"
    )


def test_unmodeled_bash_syntax_is_unknown_not_permission(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")

    _settings(config_dir, ["Bash(git diff [--cached])", "Bash(fj:*)"])
    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(report)["permissions:git"] == UNKNOWN
    assert "does not model" in _detail(report, "permissions:git")
    assert _checks(report)["permissions:fj"] == PASS
    assert report.ready is False

    _settings(
        config_dir,
        ["Bash(git:*)", "Bash(fj:*)"],
        deny=["Bash(git ?)"],
    )
    deny_unknown = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(deny_unknown)["permissions:git"] == UNKNOWN
    assert "a matching deny cannot be ruled out" in _detail(
        deny_unknown, "permissions:git"
    )
    assert _checks(deny_unknown)["permissions:fj"] == PASS
    assert deny_unknown.ready is False


def test_default_p2_workspace_is_usable_but_private_roots_are_blocked(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])
    for relative in ("state", "config", "secrets/native"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    workspace = home / "workspace"
    workspace.mkdir()

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(report)["work-path:cwd"] == PASS
    assert report.ready is True, report.to_json()

    for relative in ("", "state", "config", "secrets", "secrets/native"):
        prohibited = home / relative if relative else home
        assert prohibited.is_dir()
        blocked = _assess(
            tmp_path,
            home=home,
            config=_config(config_dir),
            workspace=prohibited,
            bin_dir=bin_dir,
        )
        assert _checks(blocked)["work-path:cwd"] == BLOCKED, relative
        assert "role and workspace roots must be separate" in _detail(
            blocked, "work-path:cwd"
        ), relative
        assert blocked.ready is False

    escape = workspace / "escape"
    escape.symlink_to(home / "secrets", target_is_directory=True)
    escaped = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=escape,
        bin_dir=bin_dir,
    )
    assert _checks(escaped)["work-path:cwd"] == BLOCKED
    assert escaped.ready is False


def test_worktree_local_settings_come_from_the_main_checkout(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, [])
    main, worktree = _linked_worktree(tmp_path)
    main_local = _local_settings(
        main / ".claude" / "settings.local.json",
        ["Bash(git:*)", "Bash(fj:*)"],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=worktree,
        bin_dir=bin_dir,
    )
    assert _checks(report)["permissions:git"] == PASS
    assert _checks(report)["permissions:fj"] == PASS
    detail = _detail(report, "permissions:git")
    assert "worktree main checkout local settings" in detail
    assert str(main_local) in detail
    assert report.ready is True, report.to_json()

    main_local.unlink()
    worktree_local = _local_settings(
        worktree / ".claude" / "settings.local.json",
        ["Bash(git:*)", "Bash(fj:*)"],
    )
    ignored = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=worktree,
        bin_dir=bin_dir,
    )
    assert _checks(ignored)["permissions:git"] == BLOCKED
    assert _checks(ignored)["permissions:fj"] == BLOCKED
    assert str(worktree_local) not in json.dumps(ignored.to_json())
    assert ignored.ready is False

    (worktree / ".git").write_text("gitdir: relative/path\n", encoding="utf-8")
    malformed = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=worktree,
        bin_dir=bin_dir,
    )
    assert _checks(malformed)["permissions"] == UNKNOWN
    assert "worktree gitdir" in _detail(malformed, "permissions")
    assert malformed.ready is False
