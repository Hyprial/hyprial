"""Shared native credential checks, without owning credentials or launch state.

Resolve context types inside calls to avoid a module initialization cycle and
retain the original context-level validation and privacy-check seams.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ..context import AgentRuntimeContext, SharedCredentialBinding


def validate_shared_credential_binding(binding: SharedCredentialBinding) -> None:
    """Fail closed unless the native path is the exact designated private link."""
    from ..context import (
        AgentRuntimeError,
        SHARED_CREDENTIAL_DIVERGED,
        SHARED_CREDENTIAL_INVALID,
        SHARED_CREDENTIAL_UNSUPPORTED,
        private_metadata_violation,
    )

    native = binding.native_path
    target = binding.target_path
    identity = f"agent {binding.actor} shared credential {native}"
    if binding.harness == "claude":
        raise AgentRuntimeError(
            f"{identity} is unsupported on macOS because Claude Code uses a "
            "config-scoped Keychain item; use the explicit OAuth-token grant",
            code=SHARED_CREDENTIAL_UNSUPPORTED,
        )
    try:
        native_metadata = native.lstat()
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} cannot be inspected: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    if not stat.S_ISLNK(native_metadata.st_mode):
        code = (
            SHARED_CREDENTIAL_DIVERGED
            if stat.S_ISREG(native_metadata.st_mode)
            else SHARED_CREDENTIAL_INVALID
        )
        raise AgentRuntimeError(
            f"{identity} is no longer the designated symbolic link",
            code=code,
        )
    try:
        raw_destination = Path(os.readlink(native))
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} cannot be read: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    destination = (
        raw_destination
        if raw_destination.is_absolute()
        else native.parent / raw_destination
    ).resolve(strict=False)
    designated = target.resolve(strict=False)
    if destination != designated:
        raise AgentRuntimeError(
            f"{identity} points to {destination}, not designated target {target}",
            code=SHARED_CREDENTIAL_DIVERGED,
        )
    try:
        target_metadata = target.lstat()
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} designated target {target} cannot be inspected: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    if stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(
        target_metadata.st_mode
    ):
        raise AgentRuntimeError(
            f"{identity} designated target {target} must be a regular file",
            code=SHARED_CREDENTIAL_INVALID,
        )
    try:
        violation = private_metadata_violation(target, target_metadata, mode=0o600)
    except OSError as error:
        raise AgentRuntimeError(
            f"{identity} designated target {target} privacy cannot be inspected: {error}",
            code=SHARED_CREDENTIAL_INVALID,
        ) from error
    if violation is not None:
        raise AgentRuntimeError(
            f"{identity} designated target {target} must be account-private ({violation})",
            code=SHARED_CREDENTIAL_INVALID,
        )
    if binding.agent_cwd is not None:
        try:
            designated.relative_to(binding.agent_cwd.resolve(strict=False))
        except ValueError:
            pass
        else:
            raise AgentRuntimeError(
                f"{identity} designated target {target} must be outside the agent cwd",
                code=SHARED_CREDENTIAL_INVALID,
            )


def validate_shared_credential_environment(
    subject: AgentRuntimeContext | SharedCredentialBinding,
    credential_environment_names: Iterable[str],
) -> None:
    """Keep grant-declared environment credentials exclusive with the link.

    The caller supplies only credential fields declared by grants. Runtime and
    generated child-environment fields are not credentials and must never be
    classified by inspecting the complete environment.
    """
    from ..context import (
        AgentRuntimeContext, AgentRuntimeError, SHARED_CREDENTIAL_CONFLICT,
    )

    binding = (
        subject.shared_credential
        if isinstance(subject, AgentRuntimeContext)
        else subject
    )
    if binding is None:
        return
    conflicting = sorted(set(credential_environment_names))
    if conflicting:
        raise AgentRuntimeError(
            f"agent {binding.actor} native shared credential and explicit "
            f"environment credential {conflicting[0]} may not coexist",
            code=SHARED_CREDENTIAL_CONFLICT,
        )


def shared_credential_status(*, registry: Any, agent: Any) -> list[dict[str, object]]:
    """Project configured link health for ``agent list`` without reading values."""
    from ..context import (
        AgentRuntimeError,
        SharedCredentialBinding,
        SHARED_CREDENTIAL_INVALID,
        _NATIVE_CREDENTIAL_NAME,
        validate_shared_credential_binding,
    )

    config = agent.config
    if config is None or not config.shared_credentials:
        return []
    try:
        agent_home = Path(registry.home_receipt(agent.actor).path)
    except (OSError, RuntimeError, ValueError) as error:
        return [
            {
                "harness": harness,
                "authMode": "native-shared-link",
                "targetPath": target,
                "status": "error",
                "code": SHARED_CREDENTIAL_INVALID,
                "error": f"agent {agent.actor} shared credential home is unavailable: {error}",
            }
            for harness, target in config.shared_credentials
        ]
    rows: list[dict[str, object]] = []
    for harness, target in config.shared_credentials:
        native = (
            agent_home
            / "secrets"
            / "native"
            / harness
            / _NATIVE_CREDENTIAL_NAME[harness]
        )
        binding = SharedCredentialBinding(
            actor=agent.actor,
            harness=harness,
            native_path=native,
            target_path=Path(target),
            agent_cwd=None if agent.cwd is None else Path(agent.cwd),
        )
        row: dict[str, object] = {
            "harness": harness,
            "authMode": "native-shared-link",
            "nativePath": str(native),
            "targetPath": target,
        }
        try:
            validate_shared_credential_binding(binding)
        except AgentRuntimeError as error:
            row.update(
                status="error",
                code=error.code or SHARED_CREDENTIAL_INVALID,
                error=str(error),
            )
        else:
            row["status"] = "warning" if harness == "pi" else "ready"
            if harness == "pi":
                row["warning"] = (
                    "Pi 0.85.1 locks the per-agent link path, so concurrent "
                    "refreshes are not mutually excluded"
                )
        rows.append(row)
    return rows
