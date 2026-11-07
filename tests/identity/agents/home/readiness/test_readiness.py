"""Behavior-level regression tests for the read-only agent readiness preflight.

Every scenario builds a clean temporary agent home and exercises the actual
composed P2 environment, not the pytest process environment.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity.impl.agents.home.config import (
    AgentConfig,
    build_native_projection,
    materialize_native_projection,
)
from hyprial.identity.impl.agents.home.readiness import (
    BLOCKED,
    PASS,
    ROLE_TEMPLATES,
    UNKNOWN,
    ReadinessReport,
    RoleTemplate,
    ToolDeclaration,
    assess_agent_readiness,
    parse_tool_declaration,
)
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services

ENTITY = "readiness-entity"
FJ_HOST = "code.hyprial.com"
FJ_IDENTITY = "gregpaws@" + FJ_HOST


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_tools(bin_dir: Path, invocation_log: Path) -> dict[str, Path]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    tools = {
        "git": _script(bin_dir / "git", "printf 'git version 2.50.0\\n'\n"),
        "node": _script(bin_dir / "node", "printf 'v24.21.0\\n'\n"),
        "hyprial": _script(
            bin_dir / "hyprial", "printf 'packageVersion: 0.5.4\\n'\n"
        ),
    }
    tools["fj"] = _script(
        bin_dir / "fj",
        f"""\
if [ "$1" = "version" ]; then
  printf 'fj v0.6.0\\n'
  exit 0
fi
if [ "$3" = "whoami" ]; then
  printf 'whoami\\n' >> '{invocation_log}'
  if [ -f "$XDG_DATA_HOME/fj-reference" ]; then
    printf 'currently signed into {FJ_IDENTITY}\\n'
    exit 0
  fi
  printf 'SENTINEL_STDERR_KEYS_FILE_MISSING\\n' >&2
  exit 1
fi
exit 2
""",
    )
    return tools


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hyprial-home" / "agents" / "worker"
    home.mkdir(parents=True)
    home.chmod(0o700)
    (home / "secrets").mkdir()
    (home / "secrets" / "sentinel.txt").write_text(
        "SENTINEL_FILE_NOT_A_SECRET", encoding="utf-8"
    )
    return home


def _settings(
    config_dir: Path,
    allow: list[str],
    *,
    deny: list[str] | None = None,
    additional: list[Path] | None = None,
) -> Path:
    path = config_dir / "native" / "claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions: dict[str, Any] = {"allow": allow}
    if deny:
        permissions["deny"] = deny
    if additional:
        permissions["additionalDirectories"] = [str(item) for item in additional]
    path.write_text(json.dumps({"permissions": permissions}), encoding="utf-8")
    return path


def _config(config_dir: Path) -> AgentConfig:
    return AgentConfig(source=str(config_dir))


def _workspace(tmp_path: Path, name: str = "workspace") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _assess(
    tmp_path: Path,
    *,
    home: Path,
    config: AgentConfig,
    workspace: Path,
    bin_dir: Path,
    role: str = "developer",
    running: bool = False,
    tools: tuple[ToolDeclaration, ...] = (),
    work_paths: tuple[str, ...] = (),
    forgejo_identity: bool = False,
    expect_login: str | None = None,
    extra_environ: dict[str, str] | None = None,
    runner: Any = None,
    which: Any = None,
) -> ReadinessReport:
    environ = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "operator-home"),
        "XDG_DATA_HOME": str(tmp_path / "operator-xdg-data"),
        "SENTINEL_ENV_NOT_A_SECRET": "SENTINEL_ENV_VALUE",
    }
    if extra_environ:
        environ.update(extra_environ)
    return assess_agent_readiness(
        actor="worker",
        harness="claude",
        role=role,
        mode="agent-home-p2",
        cwd=str(workspace),
        config=config,
        agent_home=home,
        entity_token=ENTITY,
        running=running,
        environ=environ,
        tool_profile=TEST_TOOL_PROFILE,
        declared_tools=tools,
        work_paths=work_paths,
        forgejo_identity=forgejo_identity,
        expect_login=expect_login,
        runner=runner,
        which=which,
    )


def _checks(report: ReadinessReport) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


def _detail(report: ReadinessReport, name: str) -> str:
    return next(check.detail for check in report.checks if check.name == name)


def _action(report: ReadinessReport, name: str) -> str | None:
    return next(check.action for check in report.checks if check.name == name)


def _snapshot(*roots: Path) -> list[tuple[str, int, int, int, bytes]]:
    entries: list[tuple[str, int, int, int, bytes]] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            relative = f"{root.name}/{path.relative_to(root)}"
            metadata = path.lstat()
            body = (
                path.read_bytes()
                if path.is_file() and metadata.st_size <= 65536
                else b""
            )
            entries.append(
                (
                    relative,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    body,
                )
            )
    return sorted(entries)


def test_ready_report_uses_agent_environment_and_writes_nothing(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    extra = _workspace(tmp_path, "task-extra")
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    reference = home / "state" / "xdg" / "data" / "fj-reference"
    reference.parent.mkdir(parents=True)
    reference.write_text("declared-reference\n", encoding="utf-8")
    _settings(
        config_dir,
        ["Bash(git:*)", "Bash(fj:*)", "Bash(node:*)"],
        additional=[extra],
    )

    before = _snapshot(home, config_dir, workspace, extra)
    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("node"),),
        work_paths=(str(extra),),
        forgejo_identity=True,
        expect_login="gregpaws",
    )
    after = _snapshot(home, config_dir, workspace, extra)

    assert report.ready is True, report.to_json()
    assert _checks(report)["tool:git"] == PASS
    assert _checks(report)["tool:fj"] == PASS
    assert _checks(report)["tool:node"] == PASS
    assert _checks(report)["permissions:git"] == PASS
    assert _checks(report)["work-path:1"] == PASS
    assert _checks(report)["identity:forgejo"] == PASS
    assert report.identity == FJ_IDENTITY
    serialized = json.dumps(report.to_json())
    assert "SENTINEL" not in serialized
    assert before == after, "readiness must not create, delete, or modify files"


def test_asdf_shim_in_operator_home_is_blocked_in_isolated_agent_home(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    _script(
        tools["node"],
        f"""\
if [ "$HOME" = "{operator_home}" ]; then
  printf 'v24.21.0\\n'
  exit 0
fi
printf 'SENTINEL_ASDF_STDERR\\n' >&2
exit 126
""",
    )
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)", "Bash(node:*)"])

    parent = subprocess.run(
        [str(tools["node"]), "--version"],
        env={"HOME": str(operator_home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0, "negative control: the shim works in the parent HOME"

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("node"),),
    )

    assert _checks(report)["tool:node"] == BLOCKED
    assert "exited 126" in _detail(report, "tool:node")
    assert report.ready is False
    serialized = json.dumps(report.to_json())
    assert "SENTINEL_ASDF_STDERR" not in serialized


def test_missing_required_tool_is_blocked_and_never_inferred(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])

    missing = ToolDeclaration("readiness-missing-tool")
    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(missing,),
    )

    assert _checks(report)["tool:git"] == PASS
    assert _checks(report)["tool:readiness-missing-tool"] == BLOCKED
    assert "not found on the actual launch PATH" in _detail(
        report, "tool:readiness-missing-tool"
    )
    assert "declare --tool readiness-missing-tool=" in (
        _action(report, "tool:readiness-missing-tool") or ""
    )
    assert report.ready is False


def test_short_and_absolute_permission_forms_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git = tools["git"]
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])

    blocked = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git),),
    )
    assert _checks(blocked)["permissions:git"] == BLOCKED
    assert "short and absolute forms are not interchangeable" in _detail(
        blocked, "permissions:git"
    )

    _settings(config_dir, [f"Bash({git}:*)", "Bash(fj:*)"])
    allowed = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git),),
    )
    assert _checks(allowed)["permissions:git"] == PASS


def test_deny_wins_over_allow(tmp_path: Path) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(
        config_dir,
        ["Bash(git:*)", "Bash(fj:*)"],
        deny=["Bash(git:*)"],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )

    assert _checks(report)["permissions:git"] == BLOCKED
    assert "deny wins over allow" in _detail(report, "permissions:git")
    assert _checks(report)["permissions:fj"] == PASS
    assert report.ready is False


def _developer_with_env_key(
    monkeypatch: pytest.MonkeyPatch, *keys: str
) -> None:
    template = ROLE_TEMPLATES["developer"]
    monkeypatch.setitem(
        ROLE_TEMPLATES,
        "developer",
        RoleTemplate(
            name=template.name,
            tools=template.tools,
            notes=template.notes,
            env_keys=keys,
        ),
    )


def test_env_wrapper_grant_requires_exact_key_and_exact_wrapped_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git, fj = tools["git"], tools["fj"]
    _developer_with_env_key(monkeypatch, "READINESS_TEST_KEY")
    value = "SENTINEL_ENV_GRANT_VALUE"
    _settings(
        config_dir,
        [
            f"Bash({git}:*)",
            f"Bash({fj}:*)",
            f"Bash(env READINESS_TEST_KEY={value} {git}:*)",
        ],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git), ToolDeclaration("fj", fj)),
    )

    assert report.ready is True, report.to_json()
    assert _checks(report)["permissions:git"] == PASS
    detail = _detail(report, "permissions:env")
    assert "READINESS_TEST_KEY" in detail
    assert str(git) in detail
    assert "still requires its own non-env allow rule" in detail
    assert value not in json.dumps(report.to_json())


@pytest.mark.parametrize(
    "env_rule",
    [
        "Bash(env:*)",
        "Bash(env HYPRIAL_*:*)",
        "Bash(env HYPRIAL_APP_CORE_CONFIG=*:*)",
        "Bash(env READINESS_OTHER_KEY=value {git}:*)",
        "Bash(env READINESS_TEST_KEY=value git:*)",
    ],
)
def test_env_wrapper_broad_wildcard_and_unlisted_grants_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_rule: str,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git, fj = tools["git"], tools["fj"]
    _developer_with_env_key(monkeypatch, "READINESS_TEST_KEY")
    _settings(
        config_dir,
        [
            f"Bash({git}:*)",
            f"Bash({fj}:*)",
            env_rule.format(git=git),
        ],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git), ToolDeclaration("fj", fj)),
    )

    assert _checks(report)["permissions:env"] == BLOCKED, report.to_json()
    assert "not an authorization" in (_action(report, "permissions:env") or "")
    assert report.ready is False


def test_env_wrapper_does_not_authorize_without_separate_tool_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git, fj = tools["git"], tools["fj"]
    _developer_with_env_key(monkeypatch, "READINESS_TEST_KEY")
    _settings(
        config_dir,
        [
            f"Bash({fj}:*)",
            f"Bash(env READINESS_TEST_KEY=value {git}:*)",
        ],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git), ToolDeclaration("fj", fj)),
    )

    assert _checks(report)["permissions:git"] == BLOCKED
    assert _checks(report)["permissions:env"] == BLOCKED
    assert "no separate non-env allow rule" in _detail(report, "permissions:env")


def test_env_wrapper_deny_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    git, fj = tools["git"], tools["fj"]
    _developer_with_env_key(monkeypatch, "READINESS_TEST_KEY")
    _settings(
        config_dir,
        [
            f"Bash({git}:*)",
            f"Bash({fj}:*)",
            f"Bash(env READINESS_TEST_KEY=value {git}:*)",
        ],
        deny=[f"Bash(env READINESS_TEST_KEY=value {git}:*)"],
    )

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        tools=(ToolDeclaration("git", git), ToolDeclaration("fj", fj)),
    )

    assert _checks(report)["permissions:git"] == PASS
    assert _checks(report)["permissions:env"] == BLOCKED
    assert "deny rule blocks the wrapped invocation" in _detail(
        report, "permissions:env"
    )
    assert report.ready is False


def test_role_templates_do_not_grant_env_or_wildcard_permissions() -> None:
    for template in ROLE_TEMPLATES.values():
        assert template.env_keys == ()
        assert all("*" not in key for key in template.env_keys)
        assert all(not tool.startswith("env") for tool in template.tools)


def test_unavailable_permission_introspection_is_unknown_and_not_ready(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )

    assert _checks(report)["permissions"] == UNKNOWN
    assert report.ready is False
    assert "tool existence does not grant command permission" in _detail(
        report, "permissions"
    )

    malformed = config_dir / "native" / "claude" / "settings.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not-json", encoding="utf-8")
    broken = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
    )
    assert _checks(broken)["permissions"] == UNKNOWN
    assert "not valid JSON" in _detail(broken, "permissions")
    assert broken.ready is False


def test_missing_or_wrong_forgejo_reference_fails_safely(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])

    missing = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        forgejo_identity=True,
    )
    assert _checks(missing)["identity:forgejo"] == BLOCKED
    detail = _detail(missing, "identity:forgejo")
    assert "operator-approved" in (_action(missing, "identity:forgejo") or "")
    assert "no credential was read or printed" in detail
    assert missing.identity is None
    assert "SENTINEL_STDERR_KEYS_FILE_MISSING" not in json.dumps(missing.to_json())

    reference = home / "state" / "xdg" / "data" / "fj-reference"
    reference.parent.mkdir(parents=True)
    reference.write_text("declared-reference\n", encoding="utf-8")
    mismatch = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        forgejo_identity=True,
        expect_login="somebody-else",
    )
    assert _checks(mismatch)["identity:forgejo"] == BLOCKED
    assert "no identity switch" in _detail(mismatch, "identity:forgejo")
    assert mismatch.identity == FJ_IDENTITY
    assert "SENTINEL" not in json.dumps(mismatch.to_json())


def test_roles_separate_capabilities_and_private_workspace_roots(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = home / "secrets" / "work"
    workspace.mkdir()
    bin_dir = tmp_path / "bin"
    invocation_log = tmp_path / "fj-invocations.log"
    _install_tools(bin_dir, invocation_log)
    _settings(config_dir, ["Bash(git:*)", "Bash(hyprial:*)", "Bash(fj:*)"])

    report = _assess(
        tmp_path,
        home=home,
        config=_config(config_dir),
        workspace=workspace,
        bin_dir=bin_dir,
        role="pac",
    )

    checks = _checks(report)
    assert "tool:fj" not in checks, "the pac role must not require Forgejo tooling"
    assert "identity:forgejo" not in checks, "identity is opt-in, not a pac default"
    assert checks["tool:hyprial"] == PASS
    assert checks["work-path:cwd"] == BLOCKED
    assert "role and workspace roots must be separate" in _detail(
        report, "work-path:cwd"
    )
    assert not invocation_log.exists(), "an opt-out identity probe must not run"
    assert report.ready is False


def _publish(config: AgentConfig, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = config.freeze_manifest()
    materialize_native_projection(
        config, manifest, build_native_projection(manifest, "claude"),
        actor="worker", entity_token=ENTITY, projection_root=target,
    )


def test_source_config_update_is_visible_against_running_revision(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])
    config = _config(config_dir)
    revision_one = config.freeze_manifest().revision
    projection_one = (
        home / "state" / "config" / ENTITY / revision_one / "native" / "claude"
    )
    _publish(config, projection_one)

    current = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=True,
    )
    assert _checks(current)["config"] == PASS
    assert current.config_revision == revision_one
    assert current.reload_required is False

    skill = config_dir / "shared" / "skills" / "one" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# one\n", encoding="utf-8")
    revision_two = config.freeze_manifest().revision
    assert revision_two != revision_one

    stale = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=True,
    )
    assert _checks(stale)["config"] == BLOCKED
    assert stale.config_revision == revision_two
    assert stale.reload_required is True
    assert "reload/restart this agent at idle" in (_action(stale, "config") or "")

    next_start = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=False,
    )
    assert _checks(next_start)["config"] == PASS
    assert next_start.reload_required is False

    projection_two = (
        home / "state" / "config" / ENTITY / revision_two / "native" / "claude"
    )
    _publish(config, projection_two)
    ambiguous = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=True,
    )
    assert _checks(ambiguous)["config"] == UNKNOWN
    assert ambiguous.reload_required is None


def test_running_native_settings_symlink_is_confined_to_agent_home(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(config_dir, ["Bash(git:*)", "Bash(fj:*)"])
    config = _config(config_dir)
    revision = config.freeze_manifest().revision
    projection = (
        home / "state" / "config" / ENTITY / revision / "native" / "claude"
    )
    _publish(config, projection)
    projected_settings = projection / "settings.json"
    native = home / "secrets" / "native" / "claude"
    native.mkdir(parents=True)
    native_settings = native / "settings.json"
    native_settings.symlink_to(projected_settings)

    report = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=True,
    )
    assert _checks(report)["config"] == PASS
    assert _checks(report)["permissions:git"] == PASS
    assert _checks(report)["permissions:fj"] == PASS
    assert report.ready is True

    outside = workspace / "outside-settings.json"
    outside.write_text(projected_settings.read_text(encoding="utf-8"), encoding="utf-8")
    native_settings.unlink()
    native_settings.symlink_to(outside)
    escaped = _assess(
        tmp_path,
        home=home,
        config=config,
        workspace=workspace,
        bin_dir=bin_dir,
        running=True,
    )
    assert _checks(escaped)["permissions"] == UNKNOWN
    assert "resolves outside the agent home" in _detail(escaped, "permissions")
    assert escaped.ready is False


def test_cli_is_a_gate_and_forwards_explicit_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.shell.impl.cli.runtime import entry as cli

    calls: list[tuple[str, Any]] = []
    payloads = [
        {"ok": True, "ready": True, "status": "ready", "checks": []},
        {"ok": True, "ready": False, "status": "not-ready", "checks": []},
    ]

    def request(method: str, params: Any = None) -> Any:
        calls.append((method, params))
        return payloads[len(calls) - 1]

    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    argv = [
        "agent",
        "readiness",
        "worker",
        "--role",
        "developer",
        "--tool",
        "git",
        "--work-path",
        str(tmp_path),
        "--forgejo-identity",
        "--expect-login",
        "gregpaws",
        "--timeout",
        "3",
        "--json",
    ]

    ready = runner.invoke(cli.app, argv)
    assert ready.exit_code == 0, ready.output
    assert json.loads(ready.stdout)["ready"] is True
    assert calls[-1] == (
        "agent.readiness",
        {
            "name": "worker",
            "role": "developer",
            "tools": ["git"],
            "workPaths": [str(tmp_path)],
            "forgejoIdentity": True,
            "expectLogin": "gregpaws",
            "timeoutSeconds": 3.0,
        },
    )

    not_ready = runner.invoke(cli.app, argv)
    assert not_ready.exit_code == 1
    payload = json.loads(not_ready.stdout)
    assert payload["ready"] is False
    assert payload["ok"] is False

    helped = runner.invoke(cli.app, ["agent", "--help"])
    assert helped.exit_code == 0
    assert "readiness" in helped.output


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def test_daemon_readiness_rpc_is_nonsecret_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep this RPC test independent of whether the test host is WSL.  The
    # WSL-specific child probe has focused coverage above.
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    config_dir = tmp_path / "config"
    workspace = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    tools = _install_tools(bin_dir, tmp_path / "fj-invocations.log")
    _settings(
        config_dir,
        [f"Bash({tools['git']}:*)", f"Bash({tools['fj']}:*)"],
    )
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon.sock",
        node_id="readiness-node",
        owner="readiness-owner",
        hyprial_home=tmp_path / "daemon-home",
        **_deep_module_daemon_dependencies(),
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    try:
        app.handle(
            "agent.create",
            {
                "name": "worker",
                "cwd": str(workspace),
                "config": {
                    "sources": [{"path": str(config_dir), "required": True}],
                    "discovery": "explicit-only",
                },
            },
        )
        app.handle("agent.ensure-home", {"name": "worker"})
        result = app.handle(
            "agent.readiness",
            {
                "name": "worker",
                "role": "developer",
                "harness": "claude",
                "tools": [
                    f"git={tools['git']}",
                    f"fj={tools['fj']}",
                ],
            },
        )
    finally:
        app.agents.close()

    assert result["ready"] is True, result
    assert result["mode"] == "agent-home-p2"
    assert {check["name"] for check in result["checks"]} >= {
        "home",
        "config",
        "environment",
        "tool:git",
        "tool:fj",
        "permissions:git",
        "permissions:fj",
        "work-path:cwd",
    }
    serialized = json.dumps(result)
    assert "entityToken" not in serialized
    assert "readiness-entity" not in serialized
    assert "SENTINEL" not in serialized


def test_tool_declaration_validation() -> None:
    assert parse_tool_declaration("git") == ToolDeclaration("git")
    assert parse_tool_declaration("git=/usr/bin/git") == ToolDeclaration(
        "git", Path("/usr/bin/git")
    )
    with pytest.raises(ValueError, match="absolute"):
        parse_tool_declaration("git=relative/git")
    with pytest.raises(ValueError, match="simple command"):
        parse_tool_declaration("../git")
    assert RoleTemplate("test", (), env_keys=("EXACT_KEY",)).env_keys == (
        "EXACT_KEY",
    )
    with pytest.raises(ValueError, match="exact environment variable names"):
        RoleTemplate("test", (), env_keys=("HYPRIAL_*",))
    with pytest.raises(ValueError, match="duplicates"):
        RoleTemplate("test", (), env_keys=("A", "A"))
