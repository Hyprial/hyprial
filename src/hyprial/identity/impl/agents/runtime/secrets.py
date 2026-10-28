"""Per-agent secret inventory and explicit-grant resolution.

This module never discovers credentials.  The registry supplies one named
binding for one current agent incarnation, and the resolver opens exactly that
entry.  Login and adapter credentials are intentionally not representable as
resolver sources.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from hyprial.kernel import (
    PrivateAccessUnsupported,
    harden_private_path,
    ipc_errors,
    private_metadata_violation,
)
from hyprial.identity.impl.agents.home.provisioner  import HomeReceipt

__all__ = [
    "ResolvedSecret",
    "SecretCatalogEntry",
    "SecretCustody",
    "SecretEnvironmentRejectionEvent",
    "SecretEnvironmentNameRejected",
    "SecretGrant",
    "SecretResolver",
    "SecretResolutionError",
    "SecretSource",
    "secret_environment_name_is_allowed",
    "validate_secret_environment_name",
]

_ENTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RESERVED_ENVIRONMENT_NAMES = frozenset(
    {"PATH", "HOME", "CODEX_HOME", "LANG", "TMPDIR"}
)
_DEAD_CLAUDE_OAUTH_NAME = "ANTHROPIC_OAUTH_TOKEN"
_CLAUDE_OAUTH_REPLACEMENT = "CLAUDE_CODE_OAUTH_TOKEN"


def _is_reserved_environment_name(name: str) -> bool:
    return name in _RESERVED_ENVIRONMENT_NAMES or name.startswith("HYPRIAL_")


def validate_secret_environment_name(name: object) -> None:
    """Validate one env field and expose a recordable reserved-name verdict."""

    if not isinstance(name, str) or not _ENV_PATTERN.fullmatch(name):
        raise ValueError("secret environment names must be valid env names")
    if name == _DEAD_CLAUDE_OAUTH_NAME:
        raise ValueError(
            f"secret environment name {_DEAD_CLAUDE_OAUTH_NAME} is unsupported; "
            f"use {_CLAUDE_OAUTH_REPLACEMENT}"
        )
    if _is_reserved_environment_name(name):
        raise SecretEnvironmentNameRejected(name)


def secret_environment_name_is_allowed(name: object) -> bool:
    """Return whether ``name`` is a syntactically valid, non-reserved field.

    This is a correctness policy, not an approval list.  Custody remains a
    separate judgement made by :class:`SecretSource`.
    """

    try:
        validate_secret_environment_name(name)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SecretEnvironmentRejectionEvent:
    """Recordable identity verdict for a rejected secret field."""

    action: str
    field: str
    result: str


class SecretEnvironmentNameRejected(ValueError):
    """A reserved-name rejection carrying the event fields storage must log."""

    def __init__(self, field: str) -> None:
        self.event = SecretEnvironmentRejectionEvent(
            action="rejected",
            field=field,
            result="rejected:reserved-name",
        )
        super().__init__(f"secret environment name {field} is reserved")


class SecretCustody(str, Enum):
    USER_LOGIN = "user-login"
    USER_PROVIDER = "user-provider"
    ADAPTER = "adapter"
    AGENT_PRIVATE = "agent-private"


class SecretSource(str, Enum):
    """The distributable subset; login and adapter custody are absent."""

    USER_PROVIDER = "user-provider"
    AGENT_PRIVATE = "agent-private"


@dataclass(frozen=True, slots=True)
class SecretCatalogEntry:
    """One non-secret inventory item at a typed custody level."""

    custody: SecretCustody
    entry_id: str
    path: Path
    distributable: bool

    def __post_init__(self) -> None:
        if not self.entry_id or not self.path.is_absolute():
            raise ValueError("secret catalog entry must have id and absolute path")
        expected = self.custody in {
            SecretCustody.USER_PROVIDER,
            SecretCustody.AGENT_PRIVATE,
        }
        if self.distributable is not expected:
            raise ValueError("secret catalog distributability contradicts custody")


@dataclass(frozen=True, slots=True)
class SecretGrant:
    """Non-secret registry metadata authorizing one exact source entry."""

    actor: str
    entity_token: str
    grant_id: str
    source: SecretSource
    entry_id: str
    field_name: str | None
    environment_names: tuple[str, ...]
    revision: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.actor, "actor"),
            (self.entity_token, "entity_token"),
            (self.grant_id, "grant_id"),
            (self.entry_id, "entry_id"),
        ):
            if not value:
                raise ValueError(f"secret grant {label} must not be blank")
        if not _ENTRY_PATTERN.fullmatch(self.grant_id):
            raise ValueError("secret grant id is not a safe segment")
        if not _ENTRY_PATTERN.fullmatch(self.entry_id):
            raise ValueError("secret entry id is not a safe segment")
        if self.field_name is not None and not _ENTRY_PATTERN.fullmatch(self.field_name):
            raise ValueError("secret field name is not a safe key")
        for name in self.environment_names:
            validate_secret_environment_name(name)
        if len(set(self.environment_names)) != len(self.environment_names):
            raise ValueError("secret environment names must be unique")
        if self.revision < 1:
            raise ValueError("secret revision must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """One private value; repr and equality diagnostics never reveal it."""

    grant: SecretGrant
    value: str = field(repr=False, compare=False)

    def environment(self) -> dict[str, str]:
        return {name: self.value for name in self.grant.environment_names}


class SecretResolutionError(RuntimeError):
    """Sanitized failure with public metadata and optional operator guidance."""

    code = ipc_errors.AGENT_SECRET_RESOLUTION_FAILED

    def __init__(
        self, category: str, entry_id: str, phase: str, *, guidance: str = ""
    ) -> None:
        self.category = category
        self.entry_id = entry_id
        self.phase = phase
        suffix = f"; {guidance}" if guidance else ""
        super().__init__(
            f"secret resolution {category}: entry={entry_id!r} phase={phase!r}{suffix}"
        )


class _SecretRegistry(Protocol):
    def require(self, actor: str) -> object: ...

    def secret_grant(self, actor: str, grant_id: str) -> SecretGrant | None: ...

    def home_receipt(self, actor: str, *, require_ready: bool = True) -> HomeReceipt: ...

    def secret_inventory(self, actor: str | None = None) -> tuple[SecretGrant, ...]: ...

    def repoint_legacy_secret_grants(
        self, grant: SecretGrant, values: Mapping[str, str], raw_field: str,
    ) -> None: ...


class SecretResolver:
    """Resolve only registry-selected entries under one isolated ``H``."""

    def __init__(self, hyprial_home: Path, registry: _SecretRegistry) -> None:
        root = Path(hyprial_home)
        if not root.is_absolute():
            raise ValueError("hyprial_home must be absolute")
        self.hyprial_home = root
        self.registry = registry

    def catalog_entry(
        self,
        custody: SecretCustody,
        entry_id: str,
        *,
        actor: str | None = None,
    ) -> SecretCatalogEntry:
        """Describe one named item without listing directories or reading it."""

        _validate_entry(entry_id)
        if custody is SecretCustody.USER_LOGIN:
            if entry_id != "login":
                raise ValueError("the user login catalog id is fixed")
            path = self.hyprial_home / "secrets" / "login.json"
        elif custody is SecretCustody.USER_PROVIDER:
            path = self.hyprial_home / "secrets" / "providers" / f"{entry_id}.json"
        elif custody is SecretCustody.ADAPTER:
            path = self.hyprial_home / "secrets" / f"lark-{entry_id}.json"
        else:
            if actor is None:
                raise ValueError("agent-private catalog entries require an actor")
            path = Path(self.registry.home_receipt(actor).path) / "secrets.sqlite"
        return SecretCatalogEntry(
            custody=custody,
            entry_id=entry_id,
            path=path,
            distributable=custody
            in {SecretCustody.USER_PROVIDER, SecretCustody.AGENT_PRIVATE},
        )

    def resolve(self, actor: str, grant_id: str) -> ResolvedSecret:
        agent = self.registry.require(actor)
        grant = self.registry.secret_grant(actor, grant_id)
        if grant is None:
            raise SecretResolutionError("grant-missing", grant_id, "authorize")
        entity_token = getattr(agent, "entity_token", None)
        if not isinstance(entity_token, str) or not entity_token:
            # A blank or absent incarnation token must never be stringified:
            # ``str(None) == "None"`` on both sides of the comparison below
            # would turn the incarnation fence into a constant (the
            # mixed-version NULL-token chain).  It fails closed as a stale
            # grant — no incarnation, no authorization.
            raise SecretResolutionError("grant-stale", grant.entry_id, "authorize")
        if grant.entity_token != entity_token:
            raise SecretResolutionError("grant-stale", grant.entry_id, "authorize")
        path = self._entry_path(actor, grant)
        try:
            if grant.source is SecretSource.AGENT_PRIVATE:
                grant, value = self._read_agent_value(path, grant)
            else:
                value = self._read_value(path, grant)
        except SecretResolutionError as error:
            if (
                error.category != "missing"
                or grant.source is not SecretSource.AGENT_PRIVATE
            ):
                raise
            raise SecretResolutionError(
                error.category, error.entry_id, error.phase,
                guidance=self._agent_recovery_guidance(actor, grant),
            ) from None
        return ResolvedSecret(grant, value)

    @staticmethod
    def _agent_recovery_guidance(actor: str, grant: SecretGrant) -> str:
        field_name = (
            grant.field_name
            if secret_environment_name_is_allowed(grant.field_name)
            else next(iter(grant.environment_names), "VALUE")
        )
        command = shlex.join([
            "hyprial", "agent", "secret", "set", "--agent", actor,
            "--service", grant.entry_id, "--field", field_name,
        ])
        guidance = (
            f"For an unvalidated service, pipe the value on stdin to: {command} --no-validate. "
            f"For a validated service, use: {command} (supply all required fields)."
        )
        if field_name != grant.field_name:
            regrant = [
                "hyprial", "agent", "secret", "grant", actor,
                "--grant-id", grant.grant_id, "--source", "agent-private",
                "--entry-id", grant.entry_id, "--field-name", field_name,
                "--revision", str(grant.revision + 1),
            ]
            for name in grant.environment_names:
                regrant.extend(["--environment-name", name])
            guidance += f" Then update the legacy field binding: {shlex.join(regrant)}."
        return guidance

    def write_user_provider(
        self, entry_id: str, values: Mapping[str, str]
    ) -> Path:
        """Operator-side writer for a user-owned provider entry."""

        _validate_entry(entry_id)
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise TypeError("provider secret fields and values must be strings")
        directory = self.hyprial_home / "secrets" / "providers"
        self._ensure_private_directory(directory)
        path = directory / f"{entry_id}.json"
        self._atomic_secret_write(path, json.dumps(dict(values), sort_keys=True) + "\n")
        return path

    def _read_agent_value(self, legacy_path: Path, grant: SecretGrant) -> tuple[SecretGrant, str]:
        # Import lazily: the store also uses this module's environment policy.
        from hyprial.identity.impl.agents.secret_store.legacy import migrate_entry
        from hyprial.identity.impl.agents.secret_store.store import AgentSecretStore

        home = legacy_path.parent.parent
        database = home / "secrets.sqlite"
        self._validate_ancestors(database, grant.entry_id)
        try:
            # Migration commits and verifies SQLite before deleting the source.
            # Values returned below always come from SQLite, never from a file.
            try:
                metadata = legacy_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not (grant.entry_id == "native" and stat.S_ISDIR(metadata.st_mode)):
                    store = AgentSecretStore(home)
                    fields = [
                        item.field_name for item in self.registry.secret_inventory(grant.actor)
                        if item.source is grant.source and item.entry_id == grant.entry_id
                        and item.entity_token == grant.entity_token and item.field_name is not None
                    ]
                    migrate_entry(
                        store, grant.entry_id, required_fields=fields,
                        repoint=lambda values, field: self.registry.repoint_legacy_secret_grants(
                            grant, values, field,
                        ),
                    )
            # Another resolver may have migrated and removed the file since our
            # first grant read. Never use a stale fieldless binding in that case.
            current = self.registry.secret_grant(grant.actor, grant.grant_id)
            if current is None or (
                current.entity_token, current.source, current.entry_id
            ) != (grant.entity_token, grant.source, grant.entry_id):
                raise SecretResolutionError("grant-stale", grant.entry_id, "authorize")
            grant = current
            try:
                database.lstat()
            except FileNotFoundError:
                raise SecretResolutionError("missing", grant.entry_id, "store-open") from None
            store = AgentSecretStore(home, read_only=True)
            secret = store.get(grant.entry_id, grant.field_name or "VALUE")
        except (OSError, ValueError, sqlite3.Error):
            # An unsafe, conflicting or unreadable store never enables fallback.
            raise SecretResolutionError(
                "store-unavailable", grant.entry_id, "store-read"
            ) from None
        if secret is None:
            raise SecretResolutionError("missing", grant.entry_id, "select")
        if not secret.value:
            raise SecretResolutionError("invalid", grant.entry_id, "select")
        return grant, secret.value

    def _entry_path(self, actor: str, grant: SecretGrant) -> Path:
        if grant.source is SecretSource.USER_PROVIDER:
            return self.hyprial_home / "secrets" / "providers" / f"{grant.entry_id}.json"
        if grant.source is SecretSource.AGENT_PRIVATE:
            receipt = self.registry.home_receipt(actor)
            if receipt.entity_token != grant.entity_token:
                raise SecretResolutionError("grant-stale", grant.entry_id, "home")
            return Path(receipt.path) / "secrets" / grant.entry_id
        raise SecretResolutionError("source-forbidden", grant.entry_id, "authorize")

    def _read_value(self, path: Path, grant: SecretGrant) -> str:
        self._validate_ancestors(path, grant.entry_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise SecretResolutionError("missing", grant.entry_id, "open") from None
        except PermissionError:
            raise SecretResolutionError("permission", grant.entry_id, "open") from None
        except OSError:
            raise SecretResolutionError("io", grant.entry_id, "open") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SecretResolutionError("unsafe-type", grant.entry_id, "open")
        violation = self._privacy_violation(
            path, metadata, mode=0o600, entry_id=grant.entry_id, phase="open"
        )
        if violation is not None:
            raise SecretResolutionError(violation, grant.entry_id, "open")
        try:
            text = path.read_text(encoding="utf-8")
        except PermissionError:
            raise SecretResolutionError("permission", grant.entry_id, "read") from None
        except UnicodeError:
            raise SecretResolutionError("invalid", grant.entry_id, "decode") from None
        except OSError:
            raise SecretResolutionError("io", grant.entry_id, "read") from None
        if grant.field_name is None:
            if not text:
                raise SecretResolutionError("invalid", grant.entry_id, "decode")
            return text
        try:
            document = json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            raise SecretResolutionError("invalid", grant.entry_id, "decode") from None
        if not isinstance(document, dict):
            raise SecretResolutionError("invalid", grant.entry_id, "decode")
        value = document.get(grant.field_name)
        if not isinstance(value, str) or not value:
            raise SecretResolutionError("invalid", grant.entry_id, "select")
        return value

    def _validate_ancestors(self, path: Path, entry_id: str) -> None:
        try:
            relative = path.relative_to(self.hyprial_home)
        except ValueError:
            raise SecretResolutionError("outside-home", entry_id, "locate") from None
        current = self.hyprial_home
        try:
            root_metadata = current.lstat()
        except FileNotFoundError:
            raise SecretResolutionError("missing", entry_id, "ancestor") from None
        except PermissionError:
            raise SecretResolutionError("permission", entry_id, "ancestor") from None
        except OSError:
            raise SecretResolutionError("io", entry_id, "ancestor") from None
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise SecretResolutionError("unsafe-path", entry_id, "ancestor")
        violation = self._privacy_violation(
            current, root_metadata, mode=None, entry_id=entry_id, phase="ancestor"
        )
        if violation is not None:
            raise SecretResolutionError(violation, entry_id, "ancestor")
        for part in relative.parts[:-1]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                raise SecretResolutionError("missing", entry_id, "ancestor") from None
            except PermissionError:
                raise SecretResolutionError("permission", entry_id, "ancestor") from None
            except OSError:
                raise SecretResolutionError("io", entry_id, "ancestor") from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SecretResolutionError("unsafe-path", entry_id, "ancestor")
            violation = self._privacy_violation(
                current,
                metadata,
                mode=None if current == self.hyprial_home else 0o700,
                entry_id=entry_id,
                phase="ancestor",
            )
            if violation is not None:
                raise SecretResolutionError(violation, entry_id, "ancestor")

    def _ensure_private_directory(self, directory: Path) -> None:
        self._require_private_directory(
            self.hyprial_home, directory.name, "write-home-root"
        )
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            try:
                path.mkdir(mode=0o700)
                harden_private_path(path, mode=0o700)
            except PrivateAccessUnsupported:
                raise SecretResolutionError(
                    "unsupported-private-acl", directory.name, "write-parent"
                ) from None
            except OSError:
                raise SecretResolutionError(
                    "io", directory.name, "write-parent"
                ) from None
        self._validate_ancestors(
            directory / ".prospective-entry", directory.name
        )
        self._require_private_directory(directory, directory.name, "write-parent")

    @staticmethod
    def _privacy_violation(
        path: Path,
        metadata: os.stat_result,
        *,
        mode: int | None,
        entry_id: str,
        phase: str,
    ) -> str | None:
        try:
            return private_metadata_violation(path, metadata, mode=mode)
        except OSError:
            raise SecretResolutionError("io", entry_id, phase) from None

    @staticmethod
    def _require_private_directory(path: Path, entry_id: str, phase: str) -> None:
        try:
            metadata = path.lstat()
        except OSError:
            raise SecretResolutionError("io", entry_id, phase) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SecretResolutionError("unsafe-path", entry_id, phase)
        violation = SecretResolver._privacy_violation(
            path, metadata, mode=0o700, entry_id=entry_id, phase=phase
        )
        if violation is not None:
            raise SecretResolutionError(violation, entry_id, phase)

    @staticmethod
    def _atomic_secret_write(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            if path.exists() or path.is_symlink():
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise SecretResolutionError("unsafe-type", path.name, "write")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                # Harden the empty staging file before any secret byte is
                # written: rename preserves the file's security descriptor and
                # mode, so the value is never on disk under inherited access.
                try:
                    harden_private_path(temporary, mode=0o600)
                except PrivateAccessUnsupported:
                    raise SecretResolutionError(
                        "unsupported-private-acl", path.name, "write"
                    ) from None
                except OSError:
                    raise SecretResolutionError("io", path.name, "write") from None
                with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            temporary.replace(path)
        except SecretResolutionError:
            raise
        except OSError:
            raise SecretResolutionError("io", path.name, "write") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_entry(entry_id: str) -> None:
    if not _ENTRY_PATTERN.fullmatch(entry_id):
        raise ValueError("secret entry id is not a safe segment")
