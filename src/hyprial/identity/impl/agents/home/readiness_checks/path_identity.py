"""Read-only work-path and Forgejo identity checks."""

from __future__ import annotations

import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from hyprial.identity.impl.agents.home.readiness_checks.model import (
    BLOCKED,
    PASS,
    UNKNOWN,
    ReadinessCheck,
)
from hyprial.identity.impl.agents.home.readiness_checks.probes import (
    ProbeRunner,
    _CONTROL,
    _IDENTITY,
    _PermissionModel,
    _ToolResolution,
    _bounded,
    _overlaps_private_home,
)


def _work_path_checks(
    *,
    cwd: str | None,
    work_paths: Sequence[str],
    agent_home: Path | None,
    permission_model: _PermissionModel | None,
    permission_error: str | None,
) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    if not cwd:
        return [
            ReadinessCheck(
                name="work-path:cwd",
                status=BLOCKED,
                detail="the agent has no declared cwd",
                action="set an explicit workspace cwd for the agent",
            )
        ]
    entries: list[tuple[str, str]] = []
    if not Path(cwd).is_absolute():
        checks.append(
            ReadinessCheck(
                name="work-path:cwd",
                status=BLOCKED,
                detail="the agent cwd is not absolute",
                action="set an absolute workspace cwd",
            )
        )
        cwd_path: Path | None = None
    else:
        cwd_path = Path(cwd)
        entries.append(("work-path:cwd", cwd))
    for index, path in enumerate(work_paths, start=1):
        entries.append((f"work-path:{index}", path))

    resolved_cwd = cwd_path.resolve(strict=False) if cwd_path is not None else None
    additional = (
        tuple(
            directory.resolve(strict=False)
            for directory in permission_model.additional_directories
        )
        if permission_model is not None
        else ()
    )
    seen: set[Path] = set()
    for name, raw in entries:
        path = Path(raw)
        if not path.is_absolute():
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=BLOCKED,
                    detail=(
                        f"required work path {raw!r} is not absolute; '~' is not "
                        "expanded against the operator shell"
                    ),
                    action="declare an absolute work path",
                )
            )
            continue
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if agent_home is not None and _overlaps_private_home(resolved, agent_home):
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=BLOCKED,
                    detail=(
                        f"work path {resolved} overlaps the agent's P2 private "
                        "home/secrets/state/native roots; role and workspace roots "
                        "must be separate (only the default "
                        f"{agent_home / 'workspace'} workspace is usable)"
                    ),
                    action=(
                        "use the agent's default workspace or a workspace outside "
                        "the agent home"
                    ),
                )
            )
            continue
        try:
            metadata = resolved.stat()
        except OSError:
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=BLOCKED,
                    detail=f"required work path does not exist: {resolved}",
                    action="create/provision the declared work path",
                )
            )
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=BLOCKED,
                    detail=f"required work path is not a directory: {resolved}",
                    action="declare a directory",
                )
            )
            continue
        if resolved_cwd is not None and resolved.is_relative_to(resolved_cwd):
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=PASS,
                    detail=f"{resolved} is inside the agent cwd project scope",
                )
            )
            continue
        if permission_model is None:
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=UNKNOWN,
                    detail=(
                        f"{resolved} is outside cwd and directory access cannot be "
                        f"confirmed: {permission_error or 'permission introspection unavailable'}"
                    ),
                    action=(
                        "declare the directory in permissions.additionalDirectories "
                        "using readable Claude settings"
                    ),
                )
            )
            continue
        if any(resolved.is_relative_to(directory) for directory in additional):
            checks.append(
                ReadinessCheck(
                    name=name,
                    status=PASS,
                    detail=(
                        f"{resolved} is outside cwd but declared in "
                        "permissions.additionalDirectories"
                    ),
                )
            )
            continue
        checks.append(
            ReadinessCheck(
                name=name,
                status=BLOCKED,
                detail=(
                    f"{resolved} is outside cwd and is not declared in "
                    "permissions.additionalDirectories"
                ),
                action=(
                    "declare the task directory explicitly; do not expand the "
                    "agent's workspace implicitly"
                ),
            )
        )
    return checks


def _identity_check(
    *,
    enabled: bool,
    environment: Mapping[str, str] | None,
    resolutions: Sequence[_ToolResolution],
    expected_login: str | None,
    host: str | None,
    runner: ProbeRunner,
    timeout: float,
) -> tuple[ReadinessCheck | None, str | None]:
    if not enabled:
        return None, None
    if environment is None:
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=UNKNOWN,
                detail="actual agent environment is unavailable for the identity probe",
                action="fix the home/config blockers, then rerun readiness",
            ),
            None,
        )
    host = host or environment.get("HYPRIAL_TEA_LOGIN") or "code.hyprial.com"
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=BLOCKED,
                detail=f"declared Forgejo host {host!r} is not a plain hostname",
                action="declare --forgejo-host as a hostname; no URL or path",
            ),
            None,
        )
    fj_resolution = next(
        (
            resolution
            for resolution in resolutions
            if resolution.declaration.name == "fj"
        ),
        None,
    )
    fj = fj_resolution.resolved if fj_resolution is not None else None
    if fj is None or (
        fj_resolution is not None and fj_resolution.check.status != PASS
    ):
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=BLOCKED,
                detail=(
                    "Forgejo identity was requested but the fj prerequisite did not "
                    "pass in the actual agent environment"
                ),
                action="fix the fj tool/version check first, then rerun identity",
            ),
            None,
        )
    try:
        completed = runner(
            [str(fj), "-H", host, "whoami"],
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=UNKNOWN,
                detail=f"fj whoami timed out after {timeout:g}s",
                action="check the declared Forgejo reference without interrupting work",
            ),
            None,
        )
    except OSError:
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=BLOCKED,
                detail="fj whoami could not execute in the actual agent environment",
                action="fix the fj invocation/tool path first",
            ),
            None,
        )
    if completed.returncode != 0:
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=BLOCKED,
                detail=(
                    f"fj whoami against {host} exited {completed.returncode} under "
                    "the agent's own HOME/XDG; no credential was read or printed"
                ),
                action=(
                    "configure an operator-approved single-file Forgejo reference "
                    "for this agent's XDG_DATA_HOME; do not copy the operator's "
                    "credential or point HOME/XDG at the operator"
                ),
            ),
            None,
        )
    output = _CONTROL.sub("", completed.stdout or "").strip()
    match = _IDENTITY.search(output)
    if match is None:
        fallback = re.fullmatch(r"([^\s@]+)@([^\s]+)", output)
        match = fallback
    if match is None:
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=UNKNOWN,
                detail=(
                    "fj whoami output was not recognized; identity is unconfirmed "
                    "(output was not copied into this report)"
                ),
                action="verify the fj version/reference and rerun readiness",
            ),
            None,
        )
    login = _bounded(match.group(1), 80)
    actual_host = _bounded(match.group(2), 160)
    identity = f"{login}@{actual_host}"
    if actual_host.casefold() != host.casefold():
        return (
            ReadinessCheck(
                name="identity:forgejo",
                status=BLOCKED,
                detail=(
                    f"fj reported host {actual_host!r} but {host!r} was requested"
                ),
                action="declare the intended Forgejo host explicitly",
            ),
            identity,
        )
    if expected_login is not None:
        expected = expected_login.casefold()
        observed = (
            identity.casefold() if "@" in expected_login else login.casefold()
        )
        if observed != expected:
            return (
                ReadinessCheck(
                    name="identity:forgejo",
                    status=BLOCKED,
                    detail=(
                        f"declared Forgejo identity mismatch: expected "
                        f"{expected_login}, observed {identity}; no identity switch "
                        "was attempted"
                    ),
                    action=(
                        "use the operator-approved reference for the expected "
                        "identity; do not reuse another user's credential"
                    ),
                ),
                identity,
            )
    return (
        ReadinessCheck(
            name="identity:forgejo",
            status=PASS,
            detail=(
                f"declared Forgejo identity confirmed as {identity}; no credential "
                "material was read, copied, or printed"
            ),
        ),
        identity,
    )
