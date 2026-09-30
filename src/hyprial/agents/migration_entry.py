"""Production-entry helpers for agent-home P2 migration.

The migration coordinator deliberately accepts already-authorized typed
objects.  This module owns the narrower production boundary around it: strict
operator manifests, the packaged support evidence, and private persistence of
preflight plans between daemon IPC calls.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Self

from hyprial.persistent_config import atomic_json_write

from .migration import (
    AgentMigrationAuthorization,
    AgentMigrationPlan,
    MigrationEntry,
    SupportKey,
    SupportMatrix,
)
from .registry import AgentRegistry

__all__ = [
    "AgentMigrationPlanStore",
    "MigrationAuthorizationWindow",
    "MigrationPreflightManifest",
    "load_packaged_support_matrix",
]

_SCHEMA_VERSION = 1
_MIGRATION_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class MigrationAuthorizationWindow:
    """Human authorization metadata; daemon-owned fences are added later."""

    window_id: str
    responsible_owner: str
    expires_at_ms: int

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {"windowId", "responsibleOwner", "expiresAtMs"},
            "authorizationWindow",
        )
        expires_at_ms = raw["expiresAtMs"]
        if not isinstance(expires_at_ms, int) or isinstance(expires_at_ms, bool):
            raise ValueError("authorizationWindow.expiresAtMs must be an integer")
        return cls(
            _nonempty_string(raw["windowId"], "authorizationWindow.windowId"),
            _nonempty_string(
                raw["responsibleOwner"],
                "authorizationWindow.responsibleOwner",
            ),
            expires_at_ms,
        )

    def bind(self, registry: AgentRegistry, actor: str) -> AgentMigrationAuthorization:
        """Bind an operator window to the daemon registry's current fences."""

        agent = registry.require(actor)
        receipt = registry.home_receipt(agent.actor)
        return AgentMigrationAuthorization(
            agent.actor,
            agent.entity_token,
            receipt.resource_token,
            self.window_id,
            self.responsible_owner,
            self.expires_at_ms,
        )


@dataclass(frozen=True, slots=True)
class MigrationPreflightManifest:
    """The exact, non-secret input admitted by the production preflight."""

    authorization_window: MigrationAuthorizationWindow
    entries: tuple[MigrationEntry, ...]
    required_support: tuple[SupportKey, ...]
    subject_kind: str

    @classmethod
    def from_json(cls, value: object) -> Self:
        raw = _exact_object(
            value,
            {
                "schemaVersion",
                "subjectKind",
                "authorizationWindow",
                "entries",
                "requiredSupport",
            },
            "migration manifest",
        )
        if raw["schemaVersion"] != _SCHEMA_VERSION:
            raise ValueError("unsupported migration manifest schemaVersion")
        # Allen ruled on G-R on 2026-09-29: resident agents are allowed.  Keep
        # the attestation explicit instead of inferring scope from an actor
        # name.
        if raw["subjectKind"] not in ("non-resident", "resident"):
            raise ValueError(
                "migration manifest subjectKind must be one of 'non-resident' or 'resident'"
            )
        entries = raw["entries"]
        support = raw["requiredSupport"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("migration manifest entries must be a non-empty list")
        if not isinstance(support, list) or not support:
            raise ValueError(
                "migration manifest requiredSupport must be a non-empty list"
            )
        parsed_support = tuple(SupportKey.from_json(item) for item in support)
        if len(parsed_support) != len(set(parsed_support)):
            raise ValueError("migration manifest requiredSupport must be unique")
        return cls(
            MigrationAuthorizationWindow.from_json(raw["authorizationWindow"]),
            tuple(MigrationEntry.from_json(item) for item in entries),
            parsed_support,
            raw["subjectKind"],
        )


class AgentMigrationPlanStore:
    """Private daemon-state persistence for preflighted immutable plans."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "agent-migration-plans"

    def save(self, plan: AgentMigrationPlan) -> Path:
        actor_dir = self._actor_dir(plan.authorization.actor)
        _ensure_private_directory(self.root)
        _ensure_private_directory(actor_dir)
        path = actor_dir / f"{plan.migration_id}.json"
        if path.exists():
            if self.load(plan.authorization.actor, plan.migration_id) != plan:
                raise ValueError("migration plan id is already occupied")
            return path
        atomic_json_write(path, plan.to_json())
        return path

    def load(self, actor: str, migration_id: str) -> AgentMigrationPlan:
        if _MIGRATION_ID.fullmatch(migration_id) is None:
            raise ValueError("migrationId must be 32 lowercase hexadecimal characters")
        path = self._actor_dir(actor) / f"{migration_id}.json"
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ValueError(
                f"migration plan {migration_id!r} was not found for actor {actor!r}"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("migration plan file is not a private regular file")
        try:
            plan = AgentMigrationPlan.from_json(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise ValueError("migration plan is unreadable or invalid") from error
        if (
            plan.migration_id != migration_id
            or plan.authorization.actor != actor
        ):
            raise ValueError("migration plan identity does not match its storage key")
        return plan

    def _actor_dir(self, actor: str) -> Path:
        if not actor or "/" in actor or actor in {".", ".."}:
            raise ValueError("migration plan actor is not a safe path segment")
        return self.root / actor


def load_packaged_support_matrix() -> SupportMatrix:
    """Read the reviewed P24 support evidence shipped with the package."""

    resource = files("hyprial.agents").joinpath("migration-support-matrix.json")
    return SupportMatrix.from_json(json.loads(resource.read_text(encoding="utf-8")))


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"migration plan directory is not private: {path}")


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value
