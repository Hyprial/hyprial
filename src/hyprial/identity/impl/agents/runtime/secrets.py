"""Per-agent secret inventory and explicit-grant resolution.

Runtime resolution opens only the entry named by an explicit registry grant.
Operator inventory separately enumerates user-owned model-secret metadata;
it never grants access or includes login and adapter custody.
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
    FileLockBusy,
    PrivateAccessUnsupported,
    ProcessFileLock,
    harden_private_path,
    ipc_errors,
    private_metadata_violation,
    read_private_file,
)
from hyprial.identity.impl.agents.home.provisioner  import HomeReceipt
from . import model_records

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
    "create_user_provider",
    "delete_user_provider",
    "read_user_provider_value",
    "secret_environment_name_is_allowed",
    "validate_secret_environment_name",
]

_ENTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DELETE_LOCK_TIMEOUT_SECONDS = 1.0
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
    AGENT_SHARED = "agent-shared"


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
    owner_actor: str | None = None
    owner_entity_token: str | None = None
    route_required: bool = False

    def __post_init__(self) -> None:
        if type(self.route_required) is not bool or (
            self.route_required and self.source is not SecretSource.USER_PROVIDER
        ):
            raise ValueError("only user-provider grants can require model service routing")
        if self.source is SecretSource.AGENT_SHARED:
            if not self.owner_actor or not self.owner_entity_token:
                raise ValueError("shared secret grants require an owner incarnation")
        elif self.owner_actor is not None or self.owner_entity_token is not None:
            raise ValueError("only shared secret grants can name an owner")
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
    route: model_records.ModelServiceRoute | model_records.ModelConnection | None = None

    def environment(self, *, route_admitted: bool = False) -> dict[str, str]:
        if self.route is not None and not route_admitted:
            raise SecretResolutionError("route-not-admitted", self.grant.entry_id, "launch")
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
        if grant.source is SecretSource.AGENT_SHARED:
            from hyprial.identity.impl.agents.secret_store.defaults import read_shared_value

            receipt = self.registry.home_receipt(actor)
            if receipt.entity_token != grant.entity_token:
                raise SecretResolutionError("grant-stale", grant.entry_id, "home")
            return ResolvedSecret(grant, read_shared_value(self.registry, grant))
        path = self._entry_path(actor, grant)
        route = None
        try:
            if grant.source is SecretSource.AGENT_PRIVATE:
                grant, value = self._read_agent_value(path, grant)
            else:
                value, route = self._read_value(path, grant)
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
        return ResolvedSecret(grant, value, route)

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
        try:
            model_records.write(path, values)
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "write") from None
        return path

    def create_user_provider(self, entry_id: str, values: Mapping[str, str]) -> Path:
        """Create a new user-owned provider entry; never merge into or replace one that exists.

        The create-only counterpart of :meth:`write_user_provider`, for a
        writer that must not own a name the user already has. It takes the
        same lock and performs the same validation; under the lock it either
        finds no entry file at the path and creates it, or raises
        :class:`SecretResolutionError` with category ``"exists"`` and writes
        nothing. A symlink or a directory at the path counts as "exists".
        """

        _validate_entry(entry_id)
        if any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise TypeError("provider secret fields and values must be strings")
        directory = self.hyprial_home / "secrets" / "providers"
        self._ensure_private_directory(directory)
        path = directory / f"{entry_id}.json"
        try:
            model_records.write(path, values, create_only=True)
        except FileExistsError:
            raise SecretResolutionError("exists", entry_id, "create") from None
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "create") from None
        return path

    def configure_model_service(
        self, entry_id: str, *, field_name: str, endpoint: str, protocol: str,
        vendor_id: str, value: str | None = None, create_only: bool = False,
    ) -> Path:
        """Explicitly bind one saved field to an owner-selected model service."""
        _validate_entry(entry_id)
        _validate_entry(field_name)
        if type(create_only) is not bool:
            raise ValueError("create_only must be boolean")
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError("model secret value must be a nonempty string")
        route = model_records.ModelServiceRoute(endpoint, protocol, vendor_id)
        directory = self.hyprial_home / "secrets" / "providers"
        self._ensure_private_directory(directory)
        path = directory / f"{entry_id}.json"
        try:
            model_records.write(path, {} if value is None else {field_name: value},
                                route_field=field_name, route=route, create_only=create_only)
        except FileExistsError:
            raise SecretResolutionError("exists", entry_id, "configure") from None
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "configure") from None
        return path

    def configure_model_connection(
        self, entry_id: str, *, field_name: str, connection: Mapping[str, object],
        expected_revision: int, value: str | None = None, create_only: bool = False,
    ) -> Path:
        """Persist explicit connection metadata and its key in one existing Secret record."""
        _validate_entry(entry_id)
        _validate_entry(field_name)
        model_records.configuration(connection)
        expected_revision = model_records.integer(expected_revision)
        if type(create_only) is not bool:
            raise ValueError("invalid model connection revision")
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError("model secret value must be a nonempty string")
        directory = self.hyprial_home / "secrets" / "providers"
        self._ensure_private_directory(directory)
        path = directory / f"{entry_id}.json"
        try:
            model_records.write(path, {} if value is None else {field_name: value},
                                route_field=field_name, connection=connection,
                                expected_revision=expected_revision, create_only=create_only)
        except model_records.ConnectionConflict:
            raise SecretResolutionError("revision-mismatch", entry_id, "configure") from None
        except model_records.InvalidConnection:
            raise ValueError("invalid model connection credential") from None
        except FileExistsError:
            raise SecretResolutionError("exists", entry_id, "configure") from None
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "configure") from None
        return path

    def model_connection_probe_input(
        self, entry_id: str, *, field_name: str, connection_ref: str, config_revision: int,
    ) -> dict[str, object]:
        """Resolve one operator-selected connection from one private atomic read."""
        _validate_entry(entry_id)
        _validate_entry(field_name)
        path = self.hyprial_home / "secrets" / "providers" / f"{entry_id}.json"
        try:
            self._validate_ancestors(path, entry_id)
            record = model_records.read(path)
        except FileNotFoundError:
            raise SecretResolutionError("missing", entry_id, "probe") from None
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "probe") from None
        route = record.routes.get(field_name)
        if (not isinstance(route, model_records.ModelConnection)
                or route.connection_ref != connection_ref or route.config_revision != config_revision):
            raise SecretResolutionError("revision-mismatch", entry_id, "probe")
        return {"provider": "custom", "endpoint": route.endpoint, "protocol": route.protocol,
                "secret": record.values[field_name]}

    def model_route_required(self, entry_id: str, field_name: str | None) -> bool:
        """Derive grant metadata from saved state, never from a caller's flag."""
        _validate_entry(entry_id)
        path = self.hyprial_home / "secrets" / "providers" / f"{entry_id}.json"
        try:
            self._validate_ancestors(path, entry_id)
            data, _ = read_private_file(path, max_bytes=model_records.LIMIT)
            try:
                document = json.loads(data)
            except (UnicodeError, json.JSONDecodeError):
                if field_name is None:
                    return False
                raise
            if not isinstance(document, dict) or "$schema" not in document:
                return False
            record = model_records.decode(document)
        except FileNotFoundError:
            return False
        except SecretResolutionError as error:
            if error.category == "missing":
                return False
            raise
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "grant-route") from None
        if record.configured and field_name is None:
            raise SecretResolutionError("field-required", entry_id, "grant-route")
        return field_name in record.routes

    def list_user_providers(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple((entry, fields) for entry, fields, _ in self.list_model_services())

    def model_service_metadata(self, entry_id: str) -> dict[str, object]:
        """Read one atomic record's non-secret configuration after a write."""
        _validate_entry(entry_id)
        path = self.hyprial_home / "secrets" / "providers" / f"{entry_id}.json"
        try:
            self._validate_ancestors(path, entry_id)
            record = model_records.read(path)
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", entry_id, "inventory") from None
        result: dict[str, object] = {"entryId": entry_id, "fieldNames": sorted(record.values)}
        if record.configured:
            result["routes"] = {name: route.document() for name, route in record.routes.items()
                                if isinstance(route, model_records.ModelServiceRoute)}
            connections = {name: route.document() for name, route in record.routes.items()
                           if isinstance(route, model_records.ModelConnection)}
            if connections:
                result["connections"] = connections
        return result

    def list_model_services(self, *, include_connections: bool = False) -> tuple[tuple[str, tuple[str, ...], dict[str, dict[str, object]]], ...]:
        """Read saved entry and field names, independently of agent grants.

        Invalid private files refuse the entire observation. The read never
        creates, hardens or repairs files, and never returns values or paths.
        """

        directory = self.hyprial_home / "secrets" / "providers"
        try:
            self._validate_ancestors(directory / ".inventory", "catalog")
        except SecretResolutionError as error:
            if error.category == "missing":
                return ()
            raise
        entries = []
        inspected = 0
        try:
            with os.scandir(directory) as children:
                for child in children:
                    if not child.name.endswith(".json"):
                        continue
                    entry_id = child.name[:-5]
                    if not _ENTRY_PATTERN.fullmatch(entry_id):
                        continue
                    if inspected >= 1000:
                        raise SecretResolutionError("limit", "catalog", "inventory")
                    inspected += 1
                    data, _ = read_private_file(Path(child.path), max_bytes=1024 * 1024)
                    record = model_records.decode(json.loads(data))
                    hidden = set() if include_connections else {
                        name for name, route in record.routes.items()
                        if isinstance(route, model_records.ModelConnection)
                    }
                    fields = tuple(sorted(record.values.keys() - hidden))
                    if not fields:
                        continue
                    entries.append((entry_id, fields, {
                        name: route.document() for name, route in record.routes.items()
                        if name not in hidden
                    }))
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise SecretResolutionError("unavailable", "catalog", "inventory") from None
        return tuple(sorted(entries))

    def _read_agent_value(self, legacy_path: Path, grant: SecretGrant) -> tuple[SecretGrant, str]:
        from hyprial.identity.impl.agents.secret_store.store import AgentSecretStore

        home = legacy_path.parent.parent
        database = home / "secrets.sqlite"
        self._validate_ancestors(database, grant.entry_id)
        try:
            try:
                database.lstat()
            except FileNotFoundError:
                raise SecretResolutionError("missing", grant.entry_id, "store-open") from None
            store = AgentSecretStore(home, read_only=True)
            secret = store.get(grant.entry_id, grant.field_name or "VALUE")
        except (OSError, ValueError, sqlite3.Error):
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

    def _read_value(
        self, path: Path, grant: SecretGrant,
    ) -> tuple[str, model_records.ModelServiceRoute | model_records.ModelConnection | None]:
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
            data, _ = read_private_file(path, max_bytes=model_records.LIMIT)
            text = data.decode("utf-8")
        except PermissionError:
            raise SecretResolutionError("permission", grant.entry_id, "read") from None
        except UnicodeError:
            raise SecretResolutionError("invalid", grant.entry_id, "decode") from None
        except PrivateAccessUnsupported:
            raise SecretResolutionError("unsafe-file", grant.entry_id, "read") from None
        except OSError:
            raise SecretResolutionError("io", grant.entry_id, "read") from None
        try:
            document = json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            if grant.field_name is None and not grant.route_required and text:
                return text, None
            raise SecretResolutionError("invalid", grant.entry_id, "decode") from None
        if not isinstance(document, dict) or "$schema" not in document:
            if grant.route_required:
                raise SecretResolutionError("route-mismatch", grant.entry_id, "route")
            if grant.field_name is None and text:
                return text, None
            value = document.get(grant.field_name) if isinstance(document, dict) else None
            if not isinstance(value, str) or not value:
                raise SecretResolutionError("invalid", grant.entry_id, "select")
            return value, None
        try:
            record = model_records.decode(document)
        except ValueError:
            raise SecretResolutionError("invalid", grant.entry_id, "decode")
        route = record.routes.get(grant.field_name)
        if grant.route_required is not (route is not None):
            raise SecretResolutionError("route-mismatch", grant.entry_id, "route")
        if grant.field_name is None:
            if record.configured:
                raise SecretResolutionError("field-required", grant.entry_id, "select")
            return text, None
        value = record.values.get(grant.field_name)
        if not isinstance(value, str) or not value:
            raise SecretResolutionError("invalid", grant.entry_id, "select")
        return value, route

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
            except FileExistsError:
                self._require_private_directory(path, directory.name, "write-parent")
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


def read_user_provider_value(
    hyprial_home: Path, entry_id: str, field_name: str | None = None
) -> str:
    """Read one user-provider value by ``(entryId, fieldName)`` under the resolver's rules.

    The lookup is the one :meth:`SecretResolver.resolve` performs for a
    user-provider grant: the same ancestor and file privacy checks, the same
    record decoding, the same route consistency derived from saved state, and
    the same field selection. It is meant for an owner-side daemon operation
    that is not an agent launch, so no registry grant is consulted. Failures
    raise :class:`SecretResolutionError` (never the value or a path); an
    invalid ``entry_id`` or ``field_name`` raises ``ValueError``.
    """

    resolver = SecretResolver(hyprial_home, None)  # type: ignore[arg-type]
    grant = SecretGrant(
        actor="user",
        entity_token="user",
        grant_id=entry_id,
        source=SecretSource.USER_PROVIDER,
        entry_id=entry_id,
        field_name=field_name,
        environment_names=(),
        revision=1,
        route_required=resolver.model_route_required(entry_id, field_name),
    )
    return resolver._read_value(resolver._entry_path("user", grant), grant)[0]


def create_user_provider(hyprial_home: Path, entry_id: str, values: Mapping[str, str]) -> Path:
    """Create one user-provider entry only when nothing is at its path.

    The owner-side form of :meth:`SecretResolver.create_user_provider`: the
    existence check and the write happen under the record's write lock, so an
    entry that appears between a caller's own look and this call is refused
    with category ``exists``, never merged into or replaced.
    """

    return SecretResolver(hyprial_home, None).create_user_provider(entry_id, values)  # type: ignore[arg-type]


def delete_user_provider(hyprial_home: Path, entry_id: str) -> bool:
    """Delete one user-provider entry file; ``False`` when it is already absent.

    The counterpart of :meth:`SecretResolver.write_user_provider` for an
    owner-side daemon operation. It removes exactly ``secrets/providers/
    <entry_id>.json`` under the resolver's ancestor checks, inside the
    record's write lock, and only when the path is a plain file (never a
    symlink, a directory or anything under a non-private ancestor). The
    file's own mode is not a reason to keep it: a Key that was made public
    is exactly the one to remove. Failures raise
    :class:`SecretResolutionError` (never a path); an invalid ``entry_id``
    raises ``ValueError``.
    """

    _validate_entry(entry_id)
    resolver = SecretResolver(hyprial_home, None)  # type: ignore[arg-type]
    grant = SecretGrant(
        actor="user",
        entity_token="user",
        grant_id=entry_id,
        source=SecretSource.USER_PROVIDER,
        entry_id=entry_id,
        field_name=None,
        environment_names=(),
        revision=1,
        route_required=False,
    )
    path = resolver._entry_path("user", grant)
    if not _entry_present(path, entry_id):
        return False
    resolver._validate_ancestors(path, entry_id)
    try:
        lock = ProcessFileLock(path.parent / ".write.lock")
        lock.acquire(timeout=_DELETE_LOCK_TIMEOUT_SECONDS)
    except (OSError, FileLockBusy):
        raise SecretResolutionError("unavailable", entry_id, "delete") from None
    try:
        return _delete_entry_file(path, entry_id)
    finally:
        lock.release()


def _entry_present(path: Path, entry_id: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except PermissionError:
        raise SecretResolutionError("permission", entry_id, "delete") from None
    except OSError:
        raise SecretResolutionError("io", entry_id, "delete") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecretResolutionError("unsafe-type", entry_id, "delete")
    return True


def _delete_entry_file(path: Path, entry_id: str) -> bool:
    if not _entry_present(path, entry_id):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        raise SecretResolutionError("io", entry_id, "delete") from None
    return True


def _validate_entry(entry_id: str) -> None:
    if not _ENTRY_PATTERN.fullmatch(entry_id):
        raise ValueError("secret entry id is not a safe segment")
