"""Public result types for the read-only agent readiness preflight."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PASS = "pass"
BLOCKED = "blocked"
UNKNOWN = "unknown"

READINESS_ROLES = ("developer", "pac")

READINESS_LIMITS = (
    "Read-only: no home, projection, settings, credential, or service state is "
    "created or modified.",
    "Claude permission results come from declared settings; this check does not "
    "execute Claude Code's enforcement path.",
    "The launch config revision of a running process is not recorded; stale "
    "detection compares the source revision with published projections.",
    "Forgejo identity is probed only when explicitly requested, inside the "
    "agent's own HOME/XDG/PATH; no credential file or value is read or printed.",
    "A passing result proves declared prerequisites, not that a model service "
    "or HTTP call will succeed.",
    "Env-wrapper permission rules require an exact role env-key allowlist and "
    "an exact wrapped executable; a prefix rule on env is never authorization.",
)

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ENV_KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReadinessError(ValueError):
    """A requested check is malformed; nothing was probed or changed."""


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """One user-selected capability set, not a default grant of every tool.

    ``env_keys`` is an exact, name-only allowlist for declarations of the form
    ``Bash(env KEY=VALUE /exact/tool:*)``.  It is empty by default: a role does
    not receive an env-wrapper grant unless a later policy change adds the
    exact key.  Values and wildcard keys are never part of a template.
    """

    name: str
    tools: tuple[str, ...]
    notes: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in self.env_keys:
            if not _ENV_KEY_NAME.match(key):
                raise ReadinessError(
                    "role env keys must be exact environment variable names; "
                    "wildcards and values are not allowed"
                )
        if len(set(self.env_keys)) != len(self.env_keys):
            raise ReadinessError("role env keys must not contain duplicates")


ROLE_TEMPLATES: Mapping[str, RoleTemplate] = {
    "developer": RoleTemplate(
        name="developer",
        tools=("git", "fj"),
        notes=(
            "repository workspace; Forgejo identity is checked only when "
            "explicitly requested",
        ),
    ),
    "pac": RoleTemplate(
        name="pac",
        tools=("git", "hyprial"),
        notes=(
            "PAC entry must be an explicitly declared tool path and workflow "
            "path; project-only short-command grants do not authorize absolute "
            "entry points",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """A required tool, optionally pinned to an absolute path."""

    name: str
    path: Path | None = None

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ReadinessError(
                "tool names must be simple command names; use NAME=/absolute/path "
                "to declare an absolute invocation"
            )
        if self.path is not None and not self.path.is_absolute():
            raise ReadinessError(
                f"declared path for tool {self.name!r} must be absolute"
            )


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str
    action: str | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.action is not None:
            result["action"] = self.action
        return result


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    actor: str
    harness: str
    role: str
    mode: str
    running: bool
    config_revision: str | None
    published_revisions: tuple[str, ...]
    reload_required: bool | None
    identity: str | None
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        """Ready only when every declared prerequisite passed.

        UNKNOWN is intentionally not ready: unavailable permission/config
        introspection must never be inferred as permission.
        """

        return bool(self.checks) and all(
            check.status == PASS for check in self.checks
        )

    def to_json(self) -> dict[str, object]:
        return {
            "ok": True,
            "ready": self.ready,
            "status": "ready" if self.ready else "not-ready",
            "actor": self.actor,
            "harness": self.harness,
            "role": self.role,
            "mode": self.mode,
            "running": self.running,
            "configRevision": self.config_revision,
            "publishedRevisions": list(self.published_revisions),
            "reloadRequired": self.reload_required,
            "identity": self.identity,
            "checks": [check.to_json() for check in self.checks],
            "limits": list(READINESS_LIMITS),
        }


def parse_tool_declaration(value: str) -> ToolDeclaration:
    """Parse ``NAME`` or ``NAME=/absolute/path`` from a CLI/IPC parameter."""

    if not isinstance(value, str) or not value.strip():
        raise ReadinessError("tool declaration must be a non-empty string")
    if "=" not in value:
        return ToolDeclaration(name=value)
    name, raw_path = value.split("=", 1)
    if not name or not raw_path:
        raise ReadinessError("tool declaration must be NAME or NAME=/absolute/path")
    return ToolDeclaration(name=name, path=Path(raw_path))
