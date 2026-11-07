"""Behavior-level tests for the bounded WSL interop readiness probe."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hyprial.identity.impl.agents.home.readiness import (
    BLOCKED,
    PASS,
    ReadinessReport,
    ToolDeclaration,
)

from .test_readiness import (
    _action,
    _assess,
    _checks,
    _config,
    _detail,
    _home,
    _install_tools,
    _settings,
    _workspace,
)


def _interop_report(
    tmp_path: Path,
    runner: Any,
    *,
    marker: bool = True,
    which: Any = None,
) -> ReadinessReport:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git, fj = tools["git"], tools["fj"]
    _settings(config_dir, [f"Bash({git}:*)", f"Bash({fj}:*)"])

    def default_which(name: str, path: str | None = None) -> str | None:
        if name == "cmd.exe":
            return "/usr/bin/cmd.exe"
        return shutil.which(name, path=path)

    return _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git), ToolDeclaration("fj", fj)),
        extra_environ={"WSL_DISTRO_NAME": "Ubuntu"} if marker else None,
        runner=runner,
        which=which or default_which,
    )


def _success_probe_runner(calls: list[tuple[list[str], float, dict[str, str]]]) -> Any:
    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        calls.append((list(argv), timeout, dict(env)))
        if argv[0].endswith("/git"):
            stdout = "git version 2.50.0\n"
        elif argv[0].endswith("/fj"):
            stdout = "fj v0.6.0\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return runner


def test_wsl_interop_child_is_actually_executed(tmp_path: Path) -> None:
    calls: list[tuple[list[str], float, dict[str, str]]] = []
    report = _interop_report(tmp_path, _success_probe_runner(calls))

    assert report.ready is True, report.to_json()
    assert _checks(report)["interop:wsl"] == PASS
    assert calls[0][0] == ["/usr/bin/cmd.exe", "/d", "/c", "exit", "0"]
    assert calls[0][1] == 5.0
    assert calls[0][2]["HOME"] != str(tmp_path / "operator-home")
    assert "SENTINEL_ENV_NOT_A_SECRET" not in calls[0][2]
    assert "exited 0" in _detail(report, "interop:wsl")


def test_wsl_interop_timeout_is_blocked_without_retry(tmp_path: Path) -> None:
    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        raise subprocess.TimeoutExpired(argv, timeout)

    report = _interop_report(tmp_path, runner)

    assert _checks(report)["interop:wsl"] == BLOCKED
    detail = _detail(report, "interop:wsl")
    assert "within 5s" in detail
    assert "not a path-existence problem" in detail
    action = _action(report, "interop:wsl") or ""
    assert "no retry" in action
    assert "never restarts the machine" in action
    assert report.ready is False


def test_wsl_interop_nonzero_exit_is_blocked(tmp_path: Path) -> None:
    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        return subprocess.CompletedProcess(argv, 7, "", "")

    report = _interop_report(tmp_path, runner)

    assert _checks(report)["interop:wsl"] == BLOCKED
    assert "exited 7" in _detail(report, "interop:wsl")
    assert "no retry was attempted" in _detail(report, "interop:wsl")
    assert report.ready is False


def test_wsl_interop_launch_failure_is_blocked(tmp_path: Path) -> None:
    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        raise OSError("interop unavailable")

    report = _interop_report(tmp_path, runner)

    assert _checks(report)["interop:wsl"] == BLOCKED
    assert "could not start" in _detail(report, "interop:wsl")
    assert "not a passing child probe" in (
        _action(report, "interop:wsl") or ""
    )
    assert report.ready is False


def test_wsl_cmd_resolution_and_child_probe_both_required(tmp_path: Path) -> None:
    calls: list[tuple[list[str], float, dict[str, str]]] = []

    def which(name: str, path: str | None = None) -> str | None:
        if name == "cmd.exe":
            return "/usr/bin/cmd.exe"
        return shutil.which(name, path=path)

    report = _interop_report(
        tmp_path,
        _success_probe_runner(calls),
        which=which,
    )

    assert _checks(report)["interop:wsl"] == PASS
    assert calls[0][0][0] == "/usr/bin/cmd.exe"


def test_wsl_interop_is_not_invoked_on_non_wsl_launch(tmp_path: Path) -> None:
    calls: list[tuple[list[str], float, dict[str, str]]] = []
    report = _interop_report(
        tmp_path,
        _success_probe_runner(calls),
        marker=False,
    )

    assert "interop:wsl" not in _checks(report)
    assert all("/d" not in argv for argv, _, _ in calls)
    assert report.ready is True


def test_wsl_missing_cmd_is_blocked_not_inferred(tmp_path: Path) -> None:
    calls: list[tuple[list[str], float, dict[str, str]]] = []

    def which(name: str, path: str | None = None) -> str | None:
        if name == "cmd.exe":
            return None
        return shutil.which(name, path=path)

    report = _interop_report(
        tmp_path,
        _success_probe_runner(calls),
        which=which,
    )

    assert _checks(report)["interop:wsl"] == BLOCKED
    assert "not resolvable from the actual launch PATH" in _detail(
        report, "interop:wsl"
    )
    assert all("/d" not in argv for argv, _, _ in calls)
    assert report.ready is False
