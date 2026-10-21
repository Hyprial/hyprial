"""Compose the actual agent environment and aggregate readiness checks."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.home.environment import (
    apply_runtime_environment_profile,
)
from hyprial.identity.impl.agents.home.readiness_checks.checks import (
    _config_check,
    _home_check,
)
from hyprial.identity.impl.agents.home.readiness_checks.model import (
    BLOCKED,
    PASS,
    READINESS_ROLES,
    ROLE_TEMPLATES,
    UNKNOWN,
    ReadinessCheck,
    ReadinessError,
    ReadinessReport,
    RoleTemplate,
    ToolDeclaration,
)
from hyprial.identity.impl.agents.home.readiness_checks.path_identity import (
    _identity_check,
    _work_path_checks,
)
from hyprial.identity.impl.agents.home.readiness_checks.probes import (
    ProbeRunner,
    Which,
    _default_runner,
    _permission_checks,
    _resolve_tools,
    _wsl_interop_check,
)
from hyprial.identity.impl.agents.runtime.context import (
    DEFAULT_AGENT_TOOL_PROFILE,
    AgentToolProfile,
    derive_runtime_roots,
    runtime_environment_for_roots,
)


def _merge_declarations(
    template: RoleTemplate, declared: Sequence[ToolDeclaration]
) -> tuple[ToolDeclaration, ...]:
    merged: dict[str, ToolDeclaration] = {
        name: ToolDeclaration(name=name) for name in template.tools
    }
    seen: set[str] = set()
    for declaration in declared:
        if declaration.name in seen:
            raise ReadinessError(
                f"tool {declaration.name!r} is declared more than once"
            )
        seen.add(declaration.name)
        existing = merged.get(declaration.name)
        if existing is not None and existing.path is not None:
            raise ReadinessError(
                f"tool {declaration.name!r} conflicts with the role template path"
            )
        merged[declaration.name] = declaration
    return tuple(merged.values())


def assess_agent_readiness(
    *,
    actor: str,
    harness: str,
    role: str,
    mode: str,
    cwd: str | None,
    config: AgentConfig | None,
    agent_home: Path | None,
    entity_token: str | None,
    running: bool,
    environ: Mapping[str, str],
    tool_profile: AgentToolProfile = DEFAULT_AGENT_TOOL_PROFILE,
    declared_tools: Sequence[ToolDeclaration] = (),
    work_paths: Sequence[str] = (),
    forgejo_identity: bool = False,
    expect_login: str | None = None,
    forgejo_host: str | None = None,
    runner: ProbeRunner | None = None,
    which: Which | None = None,
    timeout_seconds: float = 8.0,
) -> ReadinessReport:
    """Evaluate role readiness against the actual agent environment.

    ``mode`` is the registry's resolved home mode (``agent-home-p2`` or
    ``legacy``).  ``agent_home`` must be the P2 home receipt path, never the
    operator HOME.  No parameter accepts credential material by design.
    """

    template = ROLE_TEMPLATES.get(role)
    if template is None:
        raise ReadinessError(
            f"unknown readiness role {role!r}; choose one of "
            + ", ".join(READINESS_ROLES)
        )
    if mode == "p2":
        # ``agent_home_mode`` names the launch branch "p2"; the public
        # projection and this report use the explicit "agent-home-p2" label.
        mode = "agent-home-p2"
    if mode not in {"agent-home-p2", "legacy"}:
        raise ReadinessError(f"unsupported agent home mode {mode!r}")
    if not actor:
        raise ReadinessError("actor is required")
    if not harness:
        raise ReadinessError("harness is required")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise ReadinessError("timeout must be within (0, 60] seconds")
    if expect_login is not None and not expect_login.strip():
        raise ReadinessError("expect-login must be a non-empty string")
    if forgejo_host is not None and not forgejo_host.strip():
        raise ReadinessError("forgejo-host must be a non-empty string")

    declared = _merge_declarations(template, declared_tools)
    runner = runner or _default_runner
    which = which or shutil.which

    checks: list[ReadinessCheck] = []
    p2 = mode == "agent-home-p2"

    if p2:
        checks.append(_home_check(agent_home))
    else:
        checks.append(
            ReadinessCheck(
                name="home",
                status=UNKNOWN,
                detail=(
                    "legacy launch mode has no P2 home; HOME/XDG/native roots come "
                    "from the launch whitelist, not an agent profile"
                ),
                action="opt this agent into an explicit P2 config for isolation checks",
            )
        )

    config_check, revision, published, reload_required, _ = _config_check(
        mode=mode,
        config=config,
        agent_home=agent_home,
        entity_token=entity_token,
        harness=harness,
        running=running,
    )
    checks.append(config_check)

    environment: dict[str, str] | None = None
    roots = None
    native_root: Path | None = None
    if p2:
        if (
            agent_home is not None
            and config is not None
            and revision is not None
            and entity_token is not None
        ):
            roots = derive_runtime_roots(
                agent_home=agent_home,
                entity_token=entity_token,
                harness=harness,
                config_revision=revision,
                tool_profile_id=tool_profile.profile_id,
                config_source=Path(config.source),
            )
            native_root = roots.native_root
            try:
                environment = apply_runtime_environment_profile(
                    environ,
                    runtime_environment_for_roots(roots, harness, tool_profile),
                )
            except ValueError as error:
                checks.append(
                    ReadinessCheck(
                        name="environment",
                        status=BLOCKED,
                        detail=f"the actual agent environment could not be composed: {error}",
                        action="fix the profile/tool environment and rerun",
                    )
                )
                environment = None
            else:
                checks.append(
                    ReadinessCheck(
                        name="environment",
                        status=PASS,
                        detail=(
                            "agent-home-p2 profile composed for "
                            f"{harness}; HOME/XDG/native roots are agent-owned and "
                            "PATH is the launch environment's PATH"
                        ),
                    )
                )
        else:
            checks.append(
                ReadinessCheck(
                    name="environment",
                    status=UNKNOWN,
                    detail=(
                        "the actual agent environment cannot be composed while the "
                        "P2 home/config revision is invalid"
                    ),
                    action="fix the home/config blockers, then rerun readiness",
                )
            )
    else:
        try:
            environment = apply_runtime_environment_profile(environ, None)
        except ValueError:
            environment = None
        checks.append(
            ReadinessCheck(
                name="environment",
                status=UNKNOWN,
                detail=(
                    "legacy launch mode has no agent-owned P2 environment; tool "
                    "checks use the launch whitelist PATH"
                ),
                action="opt this agent into an explicit P2 config for real isolation checks",
            )
        )

    interop_check = _wsl_interop_check(
        environ=environ,
        environment=environment,
        which=which,
        runner=runner,
    )
    if interop_check is not None:
        checks.append(interop_check)

    resolutions, tools_check = _resolve_tools(
        declared,
        environment=environment,
        which=which,
        runner=runner,
        timeout=timeout_seconds,
    )
    checks.extend(resolution.check for resolution in resolutions)
    checks.extend(tools_check)

    permission_checks, permission_model, permission_error = _permission_checks(
        resolutions,
        harness=harness,
        running=running,
        config=config,
        native_root=native_root,
        cwd=Path(cwd) if cwd and Path(cwd).is_absolute() else None,
        agent_home=agent_home,
        env_keys=template.env_keys,
    )
    checks.extend(permission_checks)

    checks.extend(
        _work_path_checks(
            cwd=cwd,
            work_paths=work_paths,
            agent_home=agent_home if p2 else None,
            permission_model=permission_model,
            permission_error=permission_error,
        )
    )

    identity_check, identity = _identity_check(
        enabled=forgejo_identity,
        environment=environment,
        resolutions=resolutions,
        expected_login=expect_login,
        host=forgejo_host,
        runner=runner,
        timeout=timeout_seconds,
    )
    if identity_check is not None:
        checks.append(identity_check)

    return ReadinessReport(
        actor=actor,
        harness=harness,
        role=role,
        mode=mode,
        running=running,
        config_revision=revision,
        published_revisions=published,
        reload_required=reload_required,
        identity=identity,
        checks=tuple(checks),
    )
