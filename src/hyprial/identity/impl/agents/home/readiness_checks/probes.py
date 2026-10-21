"""Bounded, read-only probes for tools, permissions, paths, and identity."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.home.readiness_checks.model import (
    BLOCKED,
    PASS,
    UNKNOWN,
    ReadinessCheck,
    ToolDeclaration,
)
from hyprial.identity.impl.agents.home.readiness_checks.permission_rules import (
    EnvGrant,
    bash_rule_matches,
    bash_rule_state,
    env_deny_matches,
    env_grant,
    specific_invocation_rule,
    unknown_rule_may_target,
)

_IDENTITY = re.compile(r"signed\s+(?:in|into)\s+([^\s@]+)@([^\s]+)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f⁨⁩]")
_GITDIR_LINE = re.compile(r"^gitdir: (.+)$")
_WORKTREE_MARKER_LIMIT = 4096
_INTEROP_TIMEOUT_SECONDS = 5.0

_VERSION_ARGS: Mapping[str, tuple[str, ...]] = {
    # fj and hyprial expose version as a subcommand, not --version.
    "fj": ("version",),
    "hyprial": ("version",),
}

ProbeRunner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str, "str | None"], "str | None"]


@dataclass(frozen=True, slots=True)
class _ToolResolution:
    declaration: ToolDeclaration
    resolved: Path | None
    check: ReadinessCheck


@dataclass(frozen=True, slots=True)
class _PermissionModel:
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    additional_directories: tuple[Path, ...]
    sources: tuple[str, ...]
    default_mode: str | None


def _bounded(text: str, limit: int = 160) -> str:
    cleaned = _CONTROL.sub(" ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except OSError:
        return False


def _overlaps_private_home(path: Path, agent_home: Path) -> bool:
    """True when a work path intersects agent-private state or secrets.

    ``agent create`` provisions ``home/workspace`` as the agent's default
    workspace.  That one subtree is usable; the home root and the
    state/secrets/config/native roots remain private even though they are
    inside the same home.
    """

    home = agent_home.resolve(strict=False)
    candidate = path.resolve(strict=False)
    if candidate == home or home.is_relative_to(candidate):
        return True
    if not candidate.is_relative_to(home):
        return False
    return not candidate.is_relative_to(home / "workspace")


def _default_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _version_line(stdout: str) -> str | None:
    for line in stdout.splitlines():
        cleaned = _CONTROL.sub(" ", line).strip()
        if cleaned:
            return _bounded(cleaned)
    return None


def _resolve_tools(
    declarations: Sequence[ToolDeclaration],
    *,
    environment: Mapping[str, str] | None,
    which: Which,
    runner: ProbeRunner,
    timeout: float,
) -> tuple[tuple[_ToolResolution, ...], tuple[ReadinessCheck, ...]]:
    """Resolve and version-probe every declared tool in the actual env."""

    if environment is None:
        checks = tuple(
            ReadinessCheck(
                name=f"tool:{declaration.name}",
                status=UNKNOWN,
                detail=(
                    "cannot compose the actual agent environment until the P2 "
                    "home and config revision are valid"
                ),
                action="fix the home/config blockers, then rerun readiness",
            )
            for declaration in declarations
        )
        return (), checks

    resolutions: list[_ToolResolution] = []
    checks: list[ReadinessCheck] = []
    path_entries = environment.get("PATH")
    for declaration in declarations:
        name = declaration.name
        if declaration.path is not None:
            candidate = declaration.path
            if any(character.isspace() for character in str(candidate)):
                checks.append(
                    ReadinessCheck(
                        name=f"tool:{name}",
                        status=BLOCKED,
                        detail=(
                            "declared tool path contains whitespace, which the "
                            "supported Bash permission rule cannot express"
                        ),
                        action="install the tool at a stable path without whitespace",
                    )
                )
                resolutions.append(_ToolResolution(declaration, None, checks[-1]))
                continue
            try:
                metadata = candidate.stat()
            except OSError:
                checks.append(
                    ReadinessCheck(
                        name=f"tool:{name}",
                        status=BLOCKED,
                        detail=f"declared absolute tool path does not exist: {candidate}",
                        action=(
                            "install the declared toolchain at this path or remove "
                            "the absolute declaration"
                        ),
                    )
                )
                resolutions.append(_ToolResolution(declaration, None, checks[-1]))
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not os.access(candidate, os.X_OK)
            ):
                checks.append(
                    ReadinessCheck(
                        name=f"tool:{name}",
                        status=BLOCKED,
                        detail=f"declared absolute tool path is not executable: {candidate}",
                        action="fix the executable mode or declare another tool path",
                    )
                )
                resolutions.append(_ToolResolution(declaration, None, checks[-1]))
                continue
            resolved = candidate
            via = "declared absolute path"
        else:
            found = which(name, path=path_entries)
            if found is None:
                checks.append(
                    ReadinessCheck(
                        name=f"tool:{name}",
                        status=BLOCKED,
                        detail=(
                            "not found on the actual launch PATH; tool existence "
                            "was not inferred"
                            if path_entries
                            else "the actual launch environment has no PATH; tool "
                            "existence was not inferred"
                        ),
                        action=(
                            "add the declared toolchain bin directory to the launch "
                            f"PATH or declare --tool {name}=/absolute/path"
                        ),
                    )
                )
                resolutions.append(_ToolResolution(declaration, None, checks[-1]))
                continue
            resolved = Path(found)
            via = "actual launch PATH"

        try:
            completed = runner(
                [str(resolved), *_VERSION_ARGS.get(name, ("--version",))],
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            check = ReadinessCheck(
                name=f"tool:{name}",
                status=BLOCKED,
                detail=(
                    f"version probe timed out after {timeout:g}s using the actual "
                    "launch environment"
                ),
                action="check the toolchain shim/runtime under the isolated agent HOME",
            )
        except OSError:
            check = ReadinessCheck(
                name=f"tool:{name}",
                status=BLOCKED,
                detail=(
                    "version probe could not execute the resolved tool under the "
                    "actual launch environment"
                ),
                action="check executable permissions and shim reachability",
            )
        else:
            version = _version_line(completed.stdout or "")
            if completed.returncode != 0:
                check = ReadinessCheck(
                    name=f"tool:{name}",
                    status=BLOCKED,
                    detail=(
                        f"version probe exited {completed.returncode} under the "
                        "actual launch environment"
                    ),
                    action=(
                        "fix the toolchain for the isolated HOME/PATH; a shim that "
                        "works in the operator shell is not sufficient"
                    ),
                )
            elif version is None:
                check = ReadinessCheck(
                    name=f"tool:{name}",
                    status=UNKNOWN,
                    detail=(
                        f"resolved via {via} at {resolved}, but the version probe "
                        "reported no version"
                    ),
                    action="declare a wrapper with a bounded --version output",
                )
            else:
                check = ReadinessCheck(
                    name=f"tool:{name}",
                    status=PASS,
                    detail=f"resolved via {via} at {resolved}; version {version}",
                )
        checks.append(check)
        resolutions.append(_ToolResolution(declaration, resolved, check))

    return tuple(resolutions), tuple(checks)


def _wsl_interop_check(
    *,
    environ: Mapping[str, str],
    environment: Mapping[str, str] | None,
    which: Which,
    runner: ProbeRunner,
) -> ReadinessCheck | None:
    """Run one bounded WSL interop child when the launch process is under WSL.

    ``environ`` is the raw daemon launch environment.  The composed agent
    environment intentionally filters WSL markers, so using the raw environment
    for detection avoids inventing a platform abstraction or widening the
    environment whitelist.  Resolving ``cmd.exe`` is not evidence; only the
    child's exit status is.
    """

    if not (environ.get("WSL_INTEROP") or environ.get("WSL_DISTRO_NAME")):
        return None
    name = "interop:wsl"
    if environment is None:
        return ReadinessCheck(
            name=name,
            status=UNKNOWN,
            detail=(
                "WSL interop is indicated by the launch environment, but the "
                "actual agent environment is unavailable for a child probe"
            ),
            action="fix the home/config blockers, then rerun readiness",
        )
    command = which("cmd.exe", path=environment.get("PATH"))
    if command is None:
        return ReadinessCheck(
            name=name,
            status=BLOCKED,
            detail=(
                "cmd.exe is not resolvable from the actual launch PATH; resolving "
                "a path is not proof of WSL interop"
            ),
            action=(
                "make the Windows system directory visible on the launch PATH; do "
                "not substitute a path-existence check for a child probe"
            ),
        )
    started = time.monotonic()
    try:
        completed = runner(
            [command, "/d", "/c", "exit", "0"],
            env=environment,
            timeout=_INTEROP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ReadinessCheck(
            name=name,
            status=BLOCKED,
            detail=(
                f"WSL interop child did not exit within "
                f"{_INTEROP_TIMEOUT_SECONDS:g}s in the actual launch environment; "
                "this is not a path-existence problem"
            ),
            action=(
                "run `cmd.exe /d /c exit 0` manually in the same agent "
                "environment and fix WSL interop; readiness makes no retry and "
                "never restarts the machine"
            ),
        )
    except OSError:
        return ReadinessCheck(
            name=name,
            status=BLOCKED,
            detail=(
                "WSL interop child could not start in the actual launch "
                "environment, even though cmd.exe was resolved"
            ),
            action=(
                "fix WSL interop and the actual launch PATH; a resolved path is "
                "not a passing child probe"
            ),
        )
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return ReadinessCheck(
            name=name,
            status=BLOCKED,
            detail=(
                f"WSL interop child {command!r} exited {completed.returncode} in "
                f"{elapsed:.3f}s; no retry was attempted"
            ),
            action=(
                "run `cmd.exe /d /c exit 0` manually in the same agent "
                "environment and fix WSL interop"
            ),
        )
    return ReadinessCheck(
        name=name,
        status=PASS,
        detail=(
            f"WSL interop child {command!r} exited 0 in {elapsed:.3f}s using the "
            "actual launch environment"
        ),
    )


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None, f"cannot read settings file {path}"
    except json.JSONDecodeError:
        return None, f"settings file is not valid JSON: {path}"
    if not isinstance(data, dict):
        return None, f"settings file is not a JSON object: {path}"
    return data, None


def _read_bounded_marker(path: Path, label: str) -> tuple[str | None, str | None]:
    """Read one tiny Git marker without following a symlink."""

    try:
        metadata = path.lstat()
    except OSError:
        return None, f"{label} cannot be inspected: {path}"
    if not stat.S_ISREG(metadata.st_mode):
        return None, f"{label} must be a regular file: {path}"
    if metadata.st_size > _WORKTREE_MARKER_LIMIT:
        return None, f"{label} is larger than {_WORKTREE_MARKER_LIMIT} bytes"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError):
        return None, f"{label} cannot be read: {path}"


def _local_settings_source(
    cwd: Path, *, agent_home: Path | None
) -> tuple[tuple[str, Path] | None, str | None]:
    """Return the local Claude settings source the project actually uses.

    Claude Code reads ``.claude/settings.local.json`` from the main checkout
    when ``cwd`` is a linked worktree, not from the worktree copy.  A main
    checkout is accepted only when the standard ``.git/worktrees/<name>`` +
    ``commondir`` layout binds it to this cwd; otherwise the source is
    UNKNOWN rather than an arbitrary external file.
    """

    marker = cwd / ".git"
    if marker.is_symlink():
        return None, (
            f"project marker {marker} is a symbolic link; refusing to infer "
            "the local settings source"
        )
    if not marker.exists() or marker.is_dir():
        return ("local project settings", cwd / ".claude" / "settings.local.json"), None
    if not marker.is_file():
        return None, f"project marker {marker} is not a regular file"
    body, error = _read_bounded_marker(marker, "worktree gitdir marker")
    if error is not None:
        return None, error
    assert body is not None
    lines = body.splitlines()
    if len(lines) != 1:
        return None, f"worktree gitdir marker must contain exactly one line: {marker}"
    match = _GITDIR_LINE.fullmatch(lines[0].strip())
    if match is None:
        return None, f"worktree gitdir marker is not recognized: {marker}"
    raw_gitdir = match.group(1)
    gitdir = Path(raw_gitdir)
    if not gitdir.is_absolute():
        return None, f"worktree gitdir is not absolute: {marker}"
    if not gitdir.is_dir():
        return None, f"worktree gitdir does not exist: {gitdir}"
    commondir_file = gitdir / "commondir"
    commondir_body, error = _read_bounded_marker(commondir_file, "worktree commondir")
    if error is not None:
        return None, error
    assert commondir_body is not None
    common_lines = commondir_body.splitlines()
    if len(common_lines) != 1 or not common_lines[0].strip():
        return None, f"worktree commondir is not recognized: {commondir_file}"
    raw_common = Path(common_lines[0].strip())
    common_dir = (
        raw_common if raw_common.is_absolute() else gitdir / raw_common
    ).resolve(strict=False)
    worktrees_dir = (common_dir / "worktrees").resolve(strict=False)
    if not gitdir.resolve(strict=False).is_relative_to(worktrees_dir):
        return None, (
            f"worktree gitdir is not under {worktrees_dir}; refusing an "
            "unbound external settings source"
        )
    if common_dir.name != ".git":
        return None, (
            f"worktree common directory {common_dir} is not a .git directory; "
            "refusing to infer the main checkout"
        )
    main_checkout = common_dir.parent
    if not main_checkout.is_dir():
        return None, f"worktree main checkout does not exist: {main_checkout}"
    if agent_home is not None and _overlaps_private_home(
        main_checkout, agent_home
    ):
        return None, (
            f"worktree main checkout {main_checkout} overlaps the agent's "
            "private home/state/secrets; refusing to read it"
        )
    return (
        "worktree main checkout local settings",
        main_checkout / ".claude" / "settings.local.json",
    ), None


def _permission_model(
    *,
    running: bool,
    config: AgentConfig | None,
    native_root: Path | None,
    cwd: Path | None,
    agent_home: Path | None,
) -> tuple[_PermissionModel | None, str | None]:
    """Read declared permissions from the settings the launch actually uses.

    Only permission metadata is retained.  Settings bodies are not copied into
    the report.  Absence of introspection is an error reason that callers must
    render as UNKNOWN, never as permission.
    """

    candidates: list[tuple[str, Path]] = []
    if running and native_root is not None:
        candidates.append(("agent native settings", native_root / "settings.json"))
    elif config is not None:
        candidates.append(
            (
                "config source native settings",
                Path(config.source) / "native" / "claude" / "settings.json",
            )
        )
    if cwd is not None:
        candidates.append(("project settings", cwd / ".claude" / "settings.json"))
        local_source, local_error = _local_settings_source(
            cwd, agent_home=agent_home
        )
        if local_error is not None:
            return None, local_error
        if local_source is not None:
            candidates.append(local_source)

    allow: list[str] = []
    deny: list[str] = []
    additional: list[Path] = []
    sources: list[str] = []
    default_mode: str | None = None
    declared_any = False
    for label, path in candidates:
        target = path
        if path.is_symlink():
            # Claude's native settings entry is a managed link into the
            # immutable agent-home projection.  Accept only that link and only
            # when it resolves inside the agent home; every other settings
            # symlink remains opaque to this read-only check.
            if label != "agent native settings" or agent_home is None:
                return (
                    None,
                    f"settings source {label} is a symbolic link; refusing to introspect",
                )
            try:
                target = path.resolve(strict=True)
            except OSError:
                return None, f"settings source {label} has a broken symbolic link"
            if not target.is_file() or not _inside(target, agent_home):
                return (
                    None,
                    f"settings source {label} resolves outside the agent home",
                )
        elif not path.exists():
            continue
        data, error = _load_json_object(target)
        if error is not None:
            return None, error
        assert data is not None
        permissions = data.get("permissions")
        if permissions is None:
            continue
        if not isinstance(permissions, dict):
            return None, f"permissions in {label} is not an object"
        sources.append(f"{label} ({path})")
        if "allow" in permissions or "deny" in permissions:
            declared_any = True
        for field, target_list in (("allow", allow), ("deny", deny)):
            values = permissions.get(field)
            if values is None:
                continue
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                return None, f"permissions.{field} in {label} is not a list of strings"
            target_list.extend(values)
        directories = permissions.get("additionalDirectories")
        if directories is not None:
            if not isinstance(directories, list) or any(
                not isinstance(value, str) for value in directories
            ):
                return None, (
                    f"permissions.additionalDirectories in {label} is not a list "
                    "of strings"
                )
            for value in directories:
                # No operator-shell expansion: "~" must not silently become
                # the daemon user's HOME while the agent runs under another.
                directory = Path(value)
                if not directory.is_absolute():
                    return None, (
                        f"permissions.additionalDirectories in {label} contains a "
                        "non-absolute path"
                    )
                additional.append(directory)
        mode = permissions.get("defaultMode")
        if mode is not None:
            if not isinstance(mode, str):
                return None, f"permissions.defaultMode in {label} is not a string"
            default_mode = mode

    if not declared_any and default_mode is None:
        where = ", ".join(sources) if sources else "the selected settings sources"
        return None, (
            f"no declared permissions.allow/deny in {where}; tool existence does "
            "not grant command permission"
        )
    return (
        _PermissionModel(
            allow=tuple(allow),
            deny=tuple(deny),
            additional_directories=tuple(additional),
            sources=tuple(sources),
            default_mode=default_mode,
        ),
        None,
    )


def _env_permission_check(
    model: _PermissionModel,
    resolutions: Sequence[_ToolResolution],
    *,
    env_keys: Sequence[str],
) -> ReadinessCheck:
    """Audit env-wrapper rules; no env prefix is ever a permission grant."""

    allowed_keys = set(env_keys)
    invocations = {
        str(resolution.resolved)
        if resolution.declaration.path is not None
        else resolution.declaration.name: resolution
        for resolution in resolutions
        if resolution.resolved is not None
    }
    problems: list[str] = []
    valid: list[EnvGrant] = []
    for position, rule in enumerate(model.allow, start=1):
        is_env, grant, error = env_grant(rule)
        if not is_env:
            continue
        if error is not None or grant is None:
            problems.append(f"allow rule #{position}: {error or 'invalid env rule'}")
            continue
        missing = [key for key in grant.env_keys if key not in allowed_keys]
        if missing:
            problems.append(
                f"allow rule #{position}: env key(s) {', '.join(missing)} are not "
                "in the exact role allowlist"
            )
            continue
        if grant.wrapped_tool not in invocations:
            problems.append(
                f"allow rule #{position}: wrapped executable does not match a "
                "declared tool invocation exactly"
            )
            continue
        normal_allow = next(
            (
                candidate
                for candidate in model.allow
                if bash_rule_matches(candidate, grant.wrapped_tool)
            ),
            None,
        )
        if normal_allow is None:
            problems.append(
                f"allow rule #{position}: the wrapped executable has no separate "
                "non-env allow rule"
            )
            continue
        denied = next(
            (
                candidate
                for candidate in model.deny
                if bash_rule_matches(candidate, grant.wrapped_tool)
                or env_deny_matches(candidate, grant)
            ),
            None,
        )
        if denied is not None:
            problems.append(
                f"allow rule #{position}: a deny rule blocks the wrapped invocation"
            )
            continue
        valid.append(grant)

    action = (
        "declare exact env keys and the exact wrapped executable; a prefix rule "
        "on `env` is not an authorization"
    )
    if problems:
        return ReadinessCheck(
            name="permissions:env",
            status=BLOCKED,
            detail=(
                "env-wrapper permission audit failed: "
                + "; ".join(problems[:3])
                + ("; ..." if len(problems) > 3 else "")
            ),
            action=action,
        )
    if valid:
        keys = sorted({key for grant in valid for key in grant.env_keys})
        tools = sorted({grant.wrapped_tool for grant in valid})
        return ReadinessCheck(
            name="permissions:env",
            status=PASS,
            detail=(
                "exact env-wrapper grant validated for key(s) "
                f"{', '.join(keys) or '(none)'} and wrapped tool(s) "
                f"{', '.join(tools)}; the wrapped executable still requires its "
                "own non-env allow rule"
            ),
        )
    return ReadinessCheck(
        name="permissions:env",
        status=PASS,
        detail=(
            "no env-wrapper permission grant is declared; env prefixes are not "
            "treated as authorization"
        ),
    )


def _permission_checks(
    resolutions: Sequence[_ToolResolution],
    *,
    harness: str,
    running: bool,
    config: AgentConfig | None,
    native_root: Path | None,
    cwd: Path | None,
    agent_home: Path | None,
    env_keys: Sequence[str] = (),
) -> tuple[list[ReadinessCheck], _PermissionModel | None, str | None]:
    if harness != "claude":
        return (
            [
                ReadinessCheck(
                    name="permissions",
                    status=UNKNOWN,
                    detail=(
                        f"permission introspection is not implemented for harness "
                        f"{harness!r}; tool existence is not permission"
                    ),
                    action=(
                        "declare and verify command permissions for this harness "
                        "with its supported settings, then document the result"
                    ),
                )
            ],
            None,
            f"permission introspection is unavailable for harness {harness!r}",
        )
    if config is None:
        return (
            [
                ReadinessCheck(
                    name="permissions",
                    status=UNKNOWN,
                    detail=(
                        "legacy mode has no agent-owned settings; user-level "
                        "command permissions cannot be introspected"
                    ),
                    action=(
                        "opt this agent into an explicit P2 config, then declare "
                        "command permissions in its native Claude settings"
                    ),
                )
            ],
            None,
            "legacy mode has no agent-owned settings",
        )
    model, error = _permission_model(
        running=running,
        config=config,
        native_root=native_root,
        cwd=cwd,
        agent_home=agent_home,
    )
    if model is None or error is not None:
        return (
            [
                ReadinessCheck(
                    name="permissions",
                    status=UNKNOWN,
                    detail=error or "permission introspection is unavailable",
                    action=(
                        "provide readable Claude settings with explicit "
                        "permissions.allow/deny, then rerun readiness"
                    ),
                )
            ],
            None,
            error or "permission introspection is unavailable",
        )
    if model.default_mode == "bypassPermissions":
        return (
            [
                ReadinessCheck(
                    name="permissions",
                    status=BLOCKED,
                    detail=(
                        "settings declare permissions.defaultMode="
                        "bypassPermissions, which this preflight does not accept"
                    ),
                    action="select an explicit allow-list instead of bypassPermissions",
                )
            ],
            model,
            None,
        )

    checks: list[ReadinessCheck] = []
    for resolution in resolutions:
        declaration = resolution.declaration
        if resolution.resolved is None:
            continue
        name = declaration.name
        path = str(resolution.resolved)
        absolute_form = declaration.path is not None
        invocation = path if absolute_form else name
        other = name if absolute_form else path
        deny_rule = next(
            (rule for rule in model.deny if bash_rule_matches(rule, invocation)),
            None,
        )
        unknown_deny = next(
            (
                rule
                for rule in model.deny
                if unknown_rule_may_target(
                    rule,
                    invocation,
                    state=bash_rule_state(rule, invocation)[0],
                )
            ),
            None,
        )
        if deny_rule is not None:
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=BLOCKED,
                    detail=(
                        f"declared deny rule {deny_rule} blocks the {invocation!r} "
                        "invocation; deny wins over allow"
                    ),
                    action="remove the deny or change the declared invocation form",
                )
            )
            continue
        if unknown_deny is not None:
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=UNKNOWN,
                    detail=(
                        f"deny rule {_bounded(unknown_deny, 160)} uses syntax this "
                        "static check does not model; a matching deny cannot be "
                        "ruled out"
                    ),
                    action=(
                        "simplify the deny rule to an exact, :* prefix, or plain * "
                        "glob form and rerun readiness"
                    ),
                )
            )
            continue
        allowed = next(
            (rule for rule in model.allow if bash_rule_matches(rule, invocation)),
            None,
        )
        if allowed is not None:
            wildcard = " (user-declared wildcard rule)" if allowed == "Bash(*)" else ""
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=PASS,
                    detail=(
                        f"{invocation!r} is allowed by {allowed}{wildcard} from "
                        + ", ".join(model.sources)
                    ),
                )
            )
            continue
        alternate = next(
            (rule for rule in model.allow if bash_rule_matches(rule, other)),
            None,
        )
        if alternate is not None:
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=BLOCKED,
                    detail=(
                        f"settings allow {other!r} via {alternate}, but this task "
                        f"invokes {invocation!r}; short and absolute forms are not "
                        "interchangeable"
                    ),
                    action=(
                        f"declare Bash({invocation}:*) or switch the task to the "
                        "allowed invocation form"
                    ),
                )
            )
            continue
        specific = next(
            (
                rule
                for rule in model.allow
                if specific_invocation_rule(rule, invocation) is not None
            ),
            None,
        )
        if specific is not None:
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=UNKNOWN,
                    detail=(
                        f"only a specific invocation of {invocation!r} is allowed "
                        f"by {_bounded(specific, 160)}; readiness does not infer an "
                        "unspecified task command, so this is neither a pass nor a "
                        "block for every invocation"
                    ),
                    action=(
                        f"check the exact task command, or declare a rule that "
                        f"covers the required {invocation!r} invocation"
                    ),
                )
            )
            continue
        unknown_allow = next(
            (
                rule
                for rule in model.allow
                if unknown_rule_may_target(
                    rule,
                    invocation,
                    state=bash_rule_state(rule, invocation)[0],
                )
            ),
            None,
        )
        if unknown_allow is not None:
            checks.append(
                ReadinessCheck(
                    name=f"permissions:{name}",
                    status=UNKNOWN,
                    detail=(
                        f"allow rule {_bounded(unknown_allow, 160)} uses syntax this "
                        "static check does not model; tool existence and that rule "
                        "do not prove permission"
                    ),
                    action=(
                        "simplify the allow rule to an exact, :* prefix, or plain * "
                        "glob form and rerun readiness"
                    ),
                )
            )
            continue
        checks.append(
            ReadinessCheck(
                name=f"permissions:{name}",
                status=BLOCKED,
                detail=(
                    f"no allow rule matches the {invocation!r} invocation; the tool "
                    "exists but permission was not inferred"
                ),
                action=f"declare Bash({invocation}:*) for this role/task",
            )
        )
    checks.append(_env_permission_check(model, resolutions, env_keys=env_keys))
    return checks, model, None
