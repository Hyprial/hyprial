"""Agent-home provisioning with filesystem and receipt fences.

The registry is the authority.  This module only performs the filesystem half
of provisioning and validates the on-disk receipt mirror; callers commit the
returned receipt into the registry's existing durable lifecycle-resource
store.  A directory without that matching registry record never authorizes an
agent.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self
from uuid import uuid4

__all__ = [
    "AgentHomeError",
    "AgentHomeProvisioner",
    "HomeProvisioningAttempt",
    "HomeReceipt",
]

_RECEIPT_SCHEMA = 1
_RECEIPT_RELATIVE = Path("state") / "home-receipt.json"
_AGENT_SUBDIRECTORIES = (Path("state"), Path("config"), Path("secrets"))


class AgentHomeError(RuntimeError):
    """A sanitized home error carrying only category, actor, and phase."""

    def __init__(self, category: str, actor: str, phase: str) -> None:
        self.category = category
        self.actor = actor
        self.phase = phase
        super().__init__(
            f"agent home {category}: actor={actor!r} phase={phase!r}"
        )


@dataclass(frozen=True, slots=True)
class HomeReceipt:
    """Non-secret provisioning metadata mirrored under ``A/state``."""

    actor: str
    entity_token: str
    resource_token: str
    path: str
    owner_uid: int
    status: str = "ready"

    def __post_init__(self) -> None:
        if not self.actor or not self.entity_token or not self.resource_token:
            raise ValueError("home receipt identity must not be blank")
        if self.status not in {"ready", "revoked", "cleaned"}:
            raise ValueError("invalid home receipt status")
        if not Path(self.path).is_absolute():
            raise ValueError("home receipt path must be absolute")

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": _RECEIPT_SCHEMA,
            "actor": self.actor,
            "entityToken": self.entity_token,
            "resourceToken": self.resource_token,
            "path": self.path,
            "ownerUid": self.owner_uid,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict) or value.get("schemaVersion") != _RECEIPT_SCHEMA:
            raise ValueError("unsupported home receipt")
        owner_uid = value.get("ownerUid")
        if not isinstance(owner_uid, int):
            raise ValueError("home receipt ownerUid must be an integer")
        return cls(
            actor=_nonempty(value.get("actor"), "actor"),
            entity_token=_nonempty(value.get("entityToken"), "entityToken"),
            resource_token=_nonempty(value.get("resourceToken"), "resourceToken"),
            path=_nonempty(value.get("path"), "path"),
            owner_uid=owner_uid,
            status=_nonempty(value.get("status"), "status"),
        )


@dataclass(frozen=True, slots=True)
class HomeProvisioningAttempt:
    receipt: HomeReceipt
    created: bool


class AgentHomeProvisioner:
    """Create and validate ``H/agents/<actor>`` without following links."""

    def __init__(self, hyprial_home: Path) -> None:
        home = Path(hyprial_home)
        if not home.is_absolute():
            raise ValueError("hyprial_home must be absolute")
        self.hyprial_home = home
        self.agents_root = home / "agents"

    def provision(
        self,
        *,
        actor: str,
        entity_token: str,
        incumbent: HomeReceipt | None,
        allow_revoked: bool = False,
    ) -> HomeProvisioningAttempt:
        """Prepare one home while the caller holds the registry reservation.

        ``incumbent`` comes from the registry lifecycle-resource row, never
        from the file.  A matching active receipt may be reused; every other
        pre-existing path is a residue or collision and is refused.
        """

        self._prepare_root(actor)
        path = self.agents_root / actor
        collision = self._filesystem_alias(actor, path)
        if collision is not None and collision.name != actor:
            raise AgentHomeError("name-collision", actor, "reserve")

        if path.exists() or path.is_symlink():
            if incumbent is None:
                raise AgentHomeError("unowned-residue", actor, "reserve")
            if incumbent.actor != actor or incumbent.entity_token != entity_token:
                raise AgentHomeError("receipt-mismatch", actor, "reserve")
            ready = (
                replace(incumbent, status="ready")
                if allow_revoked and incumbent.status == "revoked"
                else incumbent
            )
            if ready.status != "ready":
                raise AgentHomeError("unowned-residue", actor, "reserve")
            self.validate(ready)
            return HomeProvisioningAttempt(ready, False)

        resource_token = uuid4().hex
        receipt = HomeReceipt(
            actor=actor,
            entity_token=entity_token,
            resource_token=resource_token,
            path=str(path),
            owner_uid=os.getuid(),
        )
        created = False
        try:
            path.mkdir(mode=0o700)
            created = True
            (path / "state").mkdir(mode=0o700)
            _write_exclusive_json(path / _RECEIPT_RELATIVE, receipt.to_json())
            for relative in _AGENT_SUBDIRECTORIES[1:]:
                (path / relative).mkdir(mode=0o700)
            self.validate(receipt)
            return HomeProvisioningAttempt(receipt, True)
        except BaseException:
            if created:
                self.compensate(HomeProvisioningAttempt(receipt, True))
            raise

    def validate(self, receipt: HomeReceipt) -> Path:
        """Return the home path only when every ownership fence still matches."""

        expected = self.agents_root / receipt.actor
        path = Path(receipt.path)
        if path != expected:
            raise AgentHomeError("receipt-mismatch", receipt.actor, "validate-path")
        self._safe_directory(self.hyprial_home, receipt.actor, "validate-home-root")
        self._safe_directory(self.agents_root, receipt.actor, "validate-agents-root")
        self._safe_directory(path, receipt.actor, "validate-agent-root", mode=0o700)
        for relative in _AGENT_SUBDIRECTORIES:
            self._safe_directory(
                path / relative,
                receipt.actor,
                f"validate-{relative.name}",
                mode=0o700,
            )
        mirrored = self._read_receipt(path / _RECEIPT_RELATIVE, receipt.actor)
        if mirrored != receipt or receipt.status != "ready":
            raise AgentHomeError("receipt-mismatch", receipt.actor, "validate-receipt")
        return path

    def compensate(self, attempt: HomeProvisioningAttempt) -> bool:
        """Remove only this attempt's still-matching, otherwise-empty home."""

        if not attempt.created:
            return False
        receipt = attempt.receipt
        root = Path(receipt.path)
        try:
            mirrored = self._read_receipt(root / _RECEIPT_RELATIVE, receipt.actor)
        except AgentHomeError:
            return False
        if mirrored.resource_token != receipt.resource_token:
            return False
        if not self._removable(root):
            return False
        try:
            (root / _RECEIPT_RELATIVE).unlink()
            for relative in reversed(_AGENT_SUBDIRECTORIES):
                (root / relative).rmdir()
            root.rmdir()
        except OSError:
            return False
        return True

    def cleanup(self, receipt: HomeReceipt, *, expected_token: str) -> HomeReceipt:
        """Remove a revoked residue iff registry and file tokens still agree."""

        if receipt.status != "revoked" or receipt.resource_token != expected_token:
            raise AgentHomeError("cleanup-fenced", receipt.actor, "cleanup")
        root = Path(receipt.path)
        mirrored = self._read_receipt(root / _RECEIPT_RELATIVE, receipt.actor)
        if mirrored.resource_token != expected_token:
            raise AgentHomeError("cleanup-fenced", receipt.actor, "cleanup-receipt")
        if not self._removable(root):
            raise AgentHomeError("cleanup-not-empty", receipt.actor, "cleanup")
        try:
            (root / _RECEIPT_RELATIVE).unlink()
            for relative in reversed(_AGENT_SUBDIRECTORIES):
                (root / relative).rmdir()
            root.rmdir()
        except OSError as error:
            raise AgentHomeError("cleanup-not-empty", receipt.actor, "cleanup") from error
        return replace(receipt, resource_token=uuid4().hex, status="cleaned")

    @staticmethod
    def _removable(root: Path) -> bool:
        try:
            root_names = {item.name for item in root.iterdir()}
            if root_names != {item.name for item in _AGENT_SUBDIRECTORIES}:
                return False
            for relative in _AGENT_SUBDIRECTORIES:
                path = root / relative
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    return False
                names = {item.name for item in path.iterdir()}
                expected = {"home-receipt.json"} if relative == Path("state") else set()
                if names != expected:
                    return False
        except OSError:
            return False
        return True

    def _prepare_root(self, actor: str) -> None:
        # The symlink/traversal fence covers the subtree this module manages --
        # H, H/agents and each agent directory are lstat-checked below and in
        # ``validate``.  H's own prefix is the operator's trusted filesystem
        # (e.g. macOS ``/tmp`` -> ``/private/tmp``), so it is not policed here;
        # this mirrors ``SecretResolver._validate_ancestors``, which likewise
        # starts at H rather than walking to the root.
        if self.hyprial_home.exists() or self.hyprial_home.is_symlink():
            self._safe_directory(self.hyprial_home, actor, "prepare-home-root")
        else:
            self.hyprial_home.mkdir(mode=0o700, parents=True)
        if self.agents_root.exists() or self.agents_root.is_symlink():
            self._safe_directory(self.agents_root, actor, "prepare-agents-root", mode=0o700)
        else:
            self.agents_root.mkdir(mode=0o700)

    def _filesystem_alias(self, actor: str, candidate: Path) -> Path | None:
        try:
            children = tuple(self.agents_root.iterdir())
        except OSError as error:
            raise AgentHomeError("io", actor, "scan-reservations") from error
        for child in children:
            try:
                if candidate.exists() and os.path.samefile(candidate, child):
                    return child
            except OSError as error:
                raise AgentHomeError("io", actor, "check-alias") from error
        return None

    @staticmethod
    def _safe_directory(
        path: Path,
        actor: str,
        phase: str,
        *,
        mode: int | None = None,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise AgentHomeError("io", actor, phase) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentHomeError("unsafe-path", actor, phase)
        if metadata.st_uid != os.getuid():
            raise AgentHomeError("wrong-owner", actor, phase)
        if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
            raise AgentHomeError("unsafe-mode", actor, phase)

    @staticmethod
    def _read_receipt(path: Path, actor: str) -> HomeReceipt:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise AgentHomeError("unsafe-path", actor, "read-receipt")
            if metadata.st_uid != os.getuid():
                raise AgentHomeError("wrong-owner", actor, "read-receipt")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AgentHomeError("unsafe-mode", actor, "read-receipt")
            return HomeReceipt.from_json(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except AgentHomeError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError("invalid-receipt", actor, "read-receipt") from error


def _write_exclusive_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"home receipt {label} must be a non-empty string")
    return value
