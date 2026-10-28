"""Agent-home provisioning with filesystem and receipt fences.

The registry is the authority.  This module only performs the filesystem half
of provisioning and validates the on-disk receipt mirror; callers commit the
returned receipt into the registry's existing durable lifecycle-resource
store.  A directory without that matching registry record never authorizes an
agent.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self
from uuid import uuid4

from hyprial.kernel import (
    PrivateAccessUnsupported,
    current_uid,
    git_subprocess_environment,
    harden_private_path,
    posix_metadata_enforced,
    private_metadata_violation,
)

__all__ = [
    "AgentHomeError",
    "AgentHomeProvisioner",
    "HomeProvisioningAttempt",
    "HomeReceipt",
    "WorkspaceSummary",
]

_RECEIPT_SCHEMA = 1
_RECEIPT_RELATIVE = Path("state") / "home-receipt.json"
_AGENT_SUBDIRECTORIES = (Path("state"), Path("config"))
_WORKSPACE_RELATIVE = Path("workspace")
# Old secret files and harness-native credentials still require safe cleanup.
_CLEANUP_DIRECTORIES = (*_AGENT_SUBDIRECTORIES, Path("secrets"), _WORKSPACE_RELATIVE)
_SECRET_FILES = {
    "secrets.sqlite", "secrets.sqlite-journal", "secrets.sqlite-wal", "secrets.sqlite-shm",
    ".secret-hmac-key", "secret-metrics.jsonl",
}


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
class HomePayloadFile:
    path: str
    size: int
    sha256: str
    mode: int


def snapshot_home_payload(root: Path, *, root_fd: int | None = None) -> tuple[HomePayloadFile, ...]:
    """Fingerprint regular files through no-follow directory descriptors."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or not hasattr(os, "fwalk"):
        raise ValueError("safe stage descriptor I/O is unsupported")
    held = os.dup(root_fd) if root_fd is not None else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rows = []
    try:
        for current, dirs, files, parent_fd in os.fwalk(".", dir_fd=held, follow_symlinks=False):
            for name in dirs:
                if stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode):
                    raise ValueError("staged payload contains a linked directory")
            for name in files:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("staged payload contains a non-regular file")
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                rows.append(HomePayloadFile(
                    (Path(current) / name).as_posix(), metadata.st_size,
                    digest, stat.S_IMODE(metadata.st_mode),
                ))
    finally:
        os.close(held)
    return tuple(sorted(rows, key=lambda item: item.path))


def _open_payload_file(root_fd: int, relative: Path) -> int:
    """Open below the held stage directory without resolving linked parents."""
    current = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
    finally:
        os.close(current)


@dataclass(frozen=True, slots=True)
class HomeProvisioningAttempt:
    receipt: HomeReceipt
    created: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    """Visible, no-follow inventory of one agent workspace.

    The initialized Git repository is the empty default-workspace skeleton, so
    its visible metadata is included in file and byte totals.
    """

    path: str
    exists: bool
    files: int
    bytes: int
    unreadable_entries: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "files": self.files,
            "bytes": self.bytes,
            "unreadableEntries": self.unreadable_entries,
        }


class AgentHomeProvisioner:
    """Create and validate ``H/agents/<actor>`` without following links."""

    def __init__(self, hyprial_home: Path) -> None:
        home = Path(hyprial_home)
        if not home.is_absolute():
            raise ValueError("hyprial_home must be absolute")
        self.hyprial_home = home
        self.agents_root = home / "agents"

    def claim_receipt(self, *, actor: str, entity_token: str) -> HomeReceipt:
        """Return the immutable receipt a registry must persist before I/O."""

        return HomeReceipt(
            actor=actor,
            entity_token=entity_token,
            resource_token=uuid4().hex,
            path=str(self.agents_root / actor),
            owner_uid=current_uid(),
        )

    def provision(
        self,
        *,
        actor: str,
        entity_token: str,
        incumbent: HomeReceipt | None,
        allow_revoked: bool = False,
        claimed_receipt: HomeReceipt | None = None,
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
            # The scan above and this test are not atomic: a concurrent create
            # of the aliasing spelling can land in between, and a
            # case-insensitive volume then answers True for either name.  Only
            # the directory's own spelling separates a collision from reuse.
            collision = self._filesystem_alias(actor, path)
            if collision is not None and collision.name != actor:
                raise AgentHomeError("name-collision", actor, "reserve")
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

        receipt = claimed_receipt or HomeReceipt(
            actor=actor,
            entity_token=entity_token,
            resource_token=uuid4().hex,
            path=str(path),
            owner_uid=current_uid(),
        )
        if (
            receipt.actor != actor
            or receipt.entity_token != entity_token
            or Path(receipt.path) != path
            or (posix_metadata_enforced() and receipt.owner_uid != current_uid())
            or receipt.status != "ready"
        ):
            raise AgentHomeError("receipt-mismatch", actor, "provision-claim")
        created = False
        try:
            self._create_home_directory(actor, path)
            created = True
            (path / "state").mkdir(mode=0o700)
            self._harden(path / "state", actor, "create-state")
            _write_exclusive_json(path / _RECEIPT_RELATIVE, receipt.to_json())
            self._harden(
                path / _RECEIPT_RELATIVE, actor, "create-receipt", mode=0o600
            )
            for relative in _AGENT_SUBDIRECTORIES[1:]:
                (path / relative).mkdir(mode=0o700)
                self._harden(path / relative, actor, f"create-{relative.name}")
            self.validate(receipt)
            return HomeProvisioningAttempt(receipt, True)
        except BaseException:
            if created:
                self.compensate(HomeProvisioningAttempt(receipt, True))
            raise

    def provision_claimed(self, receipt: HomeReceipt) -> HomeProvisioningAttempt:
        """Materialize a registry-issued receipt without inventing authority.

        The registry persists ``receipt`` before dispatching this filesystem
        effect.  Replaying the effect after a crash therefore validates the
        same token instead of allocating a second home incarnation.
        """

        path = Path(receipt.path)
        self._prepare_root(receipt.actor)
        collision = self._filesystem_alias(receipt.actor, path)
        if collision is not None and collision.name != receipt.actor:
            raise AgentHomeError("name-collision", receipt.actor, "reserve")
        if path.exists() or path.is_symlink():
            collision = self._filesystem_alias(receipt.actor, path)
            if collision is not None and collision.name != receipt.actor:
                raise AgentHomeError("name-collision", receipt.actor, "reserve")
            try:
                mirrored = self._read_receipt(
                    path / _RECEIPT_RELATIVE, receipt.actor
                )
            except AgentHomeError as error:
                if error.category in {"missing", "io", "invalid-receipt"}:
                    raise AgentHomeError(
                        "unowned-residue", receipt.actor, "reserve"
                    ) from None
                raise
            if mirrored != receipt:
                raise AgentHomeError(
                    "receipt-mismatch", receipt.actor, "provision-claim"
                )
            self.validate(receipt)
            return HomeProvisioningAttempt(receipt, False)
        return self.provision(
            actor=receipt.actor,
            entity_token=receipt.entity_token,
            incumbent=None,
            claimed_receipt=receipt,
        )

    def validate(self, receipt: HomeReceipt) -> Path:
        """Return the home path only when every ownership fence still matches."""

        expected = self.agents_root / receipt.actor
        path = Path(receipt.path)
        if path != expected:
            raise AgentHomeError("receipt-mismatch", receipt.actor, "validate-path")
        self.safe_directory(self.hyprial_home, receipt.actor, "validate-home-root")
        self.safe_directory(self.agents_root, receipt.actor, "validate-agents-root")
        self.safe_directory(path, receipt.actor, "validate-agent-root", mode=0o700)
        for relative in _AGENT_SUBDIRECTORIES:
            self.safe_directory(
                path / relative,
                receipt.actor,
                f"validate-{relative.name}",
                mode=0o700,
            )
        workspace = path / _WORKSPACE_RELATIVE
        if workspace.exists() or workspace.is_symlink():
            self.safe_directory(
                workspace,
                receipt.actor,
                "validate-workspace",
                mode=0o700,
            )
        mirrored = self._read_receipt(path / _RECEIPT_RELATIVE, receipt.actor)
        if mirrored != receipt or receipt.status != "ready":
            raise AgentHomeError("receipt-mismatch", receipt.actor, "validate-receipt")
        return path

    def materialise_payload(
        self, receipt: HomeReceipt, staged_root: Path,
        expected_files: tuple[HomePayloadFile, ...],
    ) -> Path:
        """Import staged bytes while this home's existing FS owner holds custody.

        AgentHomeEffectLane serializes this operation with provisioning and
        cleanup. Registry-only callers hold their existing writer transaction.
        """
        home = self.validate(receipt)
        if not staged_root.is_dir() or staged_root.is_symlink():
            raise AgentHomeError("unsafe-payload", receipt.actor, "import-home")
        # Capability failure is terminal: no fallback to path-based move/copy.
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            raise AgentHomeError("unsupported-safe-payload-io", receipt.actor, "import-home")
        root_fd = os.open(staged_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        allowed = {item.name for item in _CLEANUP_DIRECTORIES}
        copied = False
        try:
            origin = os.fstat(root_fd)
            if snapshot_home_payload(staged_root, root_fd=root_fd) != tuple(sorted(expected_files, key=lambda item: item.path)):
                raise AgentHomeError("payload-changed", receipt.actor, "import-home")
            for item in expected_files:
                relative = Path(item.path)
                if relative == _RECEIPT_RELATIVE:
                    continue  # Keep the target's own custody receipt.
                if not relative.parts or relative.parts[0] not in allowed or ".." in relative.parts:
                    raise ValueError(f"payload {relative.as_posix()!r} is outside the home topology")
                source_fd = _open_payload_file(root_fd, relative)
                with os.fdopen(source_fd, "rb") as source:
                    metadata = os.fstat(source.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != item.size or stat.S_IMODE(metadata.st_mode) != item.mode:
                        raise AgentHomeError("payload-changed", receipt.actor, "import-home")
                    # Destination lifecycle is serialized by this home owner;
                    # source I/O uses the opened FD, never a re-resolved path.
                    target = home / relative
                    self._ensure_payload_parents(home, target.parent)
                    target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, item.mode)
                    with os.fdopen(target_fd, "wb") as destination:
                        os.fchmod(destination.fileno(), item.mode)
                        shutil.copyfileobj(source, destination)
                copied = True
            if not copied:
                raise ValueError("the bundle carries no payload outside its custody receipt")
            # Also reject namespace changes after safe FD acquisition. This is
            # an integrity check, not the mechanism preventing escaped I/O.
            named = os.stat(staged_root, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (origin.st_dev, origin.st_ino):
                raise AgentHomeError("stage-replaced", receipt.actor, "import-home-complete")
            if snapshot_home_payload(staged_root, root_fd=root_fd) != tuple(sorted(expected_files, key=lambda item: item.path)):
                raise AgentHomeError("payload-changed", receipt.actor, "import-home-complete")
        finally:
            os.close(root_fd)
        expected = {item.path: item for item in expected_files if item.path != _RECEIPT_RELATIVE.as_posix()}
        actual = {item.path: item for item in snapshot_home_payload(home)}
        if any(actual.get(path) != item for path, item in expected.items()):
            raise AgentHomeError("payload-changed", receipt.actor, "import-home-complete")
        self.validate(receipt)
        return home

    def _ensure_payload_parents(self, home: Path, parent: Path) -> None:
        """Create every missing level between ``home`` and ``parent`` as 0700.

        ``Path.mkdir(mode=0o700, parents=True)`` applies ``mode`` to the leaf
        only: an intermediate directory under a topology root (``secrets/native``
        for a native credential, ``state/sessions`` for a session file) came
        out at the umbrella default.  The P2 runtime owner validates the whole
        chain from the home to each root at 0700, so a landed claude home with
        a native credential was refused as ``runtime directory native is not
        private`` -- before any launch.  Only levels below a topology root are
        created here (the roots themselves are provisioned 0700 and validated),
        and each is explicitly private.  C3-S2-C found this live; see
        ``docs/notes/c3-claude-run.txt``.
        """

        missing: list[Path] = []
        current = parent
        while current != home and not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)

    def verify_payload(
        self, receipt: HomeReceipt, expected_files: tuple[HomePayloadFile, ...],
    ) -> str:
        """Read-only classification; invalid custody/residue is never absence.

        The source receipt is excluded because the target owns its own receipt.
        Only a validated empty target is absent; an incomplete/malformed home
        is partial and requires fenced reconciliation, not blind resubmission.
        Extra target files are allowed just as in materialise_payload; expected
        files must all match size, mode and digest for present.
        """
        expected = {}
        for item in expected_files:
            relative = Path(item.path)
            if (
                relative.is_absolute() or ".." in relative.parts
                or not relative.parts or relative.parts[0] not in {"state", "config", "secrets", "workspace"}
                or relative.as_posix() != item.path or item.path in expected
                or item.size < 0 or not 0 <= item.mode <= 0o777
                or len(item.sha256) != 64 or any(c not in "0123456789abcdef" for c in item.sha256)
            ):
                raise ValueError("invalid landing payload manifest")
            expected[item.path] = item
        expected.pop(_RECEIPT_RELATIVE.as_posix(), None)
        if not expected:
            raise ValueError("landing payload manifest has no payload files")
        try:
            home = self.validate(receipt)
            root_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                origin = os.fstat(root_fd)
                actual = {item.path: item for item in snapshot_home_payload(home, root_fd=root_fd)}
                self.validate(receipt)
                named = os.stat(home, follow_symlinks=False)
                if (origin.st_dev, origin.st_ino) != (named.st_dev, named.st_ino):
                    return "partial"
            finally:
                os.close(root_fd)
        except (OSError, ValueError, AgentHomeError):
            return "partial"
        actual.pop(_RECEIPT_RELATIVE.as_posix(), None)
        if not actual:
            return "absent"
        if all(actual.get(path) == item for path, item in expected.items()):
            return "present"
        return "partial"

    def ensure_workspace(self, receipt: HomeReceipt) -> Path:
        """Create the private default workspace as its own Git working tree."""

        root = self.validate(receipt)
        workspace = root / _WORKSPACE_RELATIVE
        try:
            workspace.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise AgentHomeError("io", receipt.actor, "workspace-create") from error
        else:
            self._harden(workspace, receipt.actor, "workspace-create")
        self.safe_directory(
            workspace,
            receipt.actor,
            "validate-workspace",
            mode=0o700,
        )
        self._ensure_workspace_repository(workspace, receipt.actor)
        return workspace

    @staticmethod
    def _ensure_workspace_repository(workspace: Path, actor: str) -> None:
        """Make only the agent-owned default workspace a Git toplevel."""

        workspace_root = workspace.resolve(strict=True)
        marker = workspace_root / ".git"
        environment = git_subprocess_environment()

        def discover() -> Path | None:
            try:
                completed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workspace_root),
                        "rev-parse",
                        "--show-toplevel",
                    ],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AgentHomeError("io", actor, "workspace-git-discover") from error
            line = completed.stdout.strip()
            if completed.returncode != 0 or not line or "\n" in line:
                return None
            try:
                return Path(line).resolve(strict=True)
            except OSError:
                return None

        def owns_git_boundary() -> bool:
            if marker.is_symlink() or not marker.is_dir():
                return False
            try:
                marker.resolve(strict=True).relative_to(workspace_root)
            except (OSError, ValueError):
                return False
            return True

        if discover() == workspace_root:
            if owns_git_boundary():
                return
            raise AgentHomeError("unsafe", actor, "workspace-git-boundary")
        if marker.exists() or marker.is_symlink():
            raise AgentHomeError("unsafe", actor, "workspace-git-boundary")
        try:
            initialized = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace_root),
                    f"--git-dir={marker}",
                    f"--work-tree={workspace_root}",
                    "init",
                    "--quiet",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentHomeError("io", actor, "workspace-git-init") from error
        if (
            initialized.returncode != 0
            or discover() != workspace_root
            or not owns_git_boundary()
        ):
            raise AgentHomeError("io", actor, "workspace-git-init")

    def workspace_summary(self, receipt: HomeReceipt) -> WorkspaceSummary:
        """Count visible workspace files and bytes without following symlinks."""

        root = self.validate(receipt)
        workspace = root / _WORKSPACE_RELATIVE
        if not workspace.exists() and not workspace.is_symlink():
            return WorkspaceSummary(str(workspace), False, 0, 0)
        self.safe_directory(
            workspace,
            receipt.actor,
            "validate-workspace",
            mode=0o700,
        )
        files = 0
        total_bytes = 0
        unreadable_entries = 0
        pending = [workspace]
        while pending:
            directory = pending.pop()
            try:
                entries = tuple(os.scandir(directory))
            except OSError:
                unreadable_entries += 1
                continue
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    # The directory entry itself was visible even though its
                    # metadata was not. Count it, but do not invent a size or
                    # descend through an object whose type is unknown.
                    files += 1
                    unreadable_entries += 1
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                    continue
                files += 1
                total_bytes += metadata.st_size
        return WorkspaceSummary(
            str(workspace), True, files, total_bytes, unreadable_entries
        )

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
            for relative in reversed(_CLEANUP_DIRECTORIES):
                directory = root / relative
                if directory.exists():
                    directory.rmdir()
            root.rmdir()
        except OSError:
            return False
        return True

    def cleanup(self, receipt: HomeReceipt, *, expected_token: str) -> HomeReceipt:
        """Remove a revoked home iff its durable and mirrored tokens agree.

        Credential-bearing content is the thing this operation exists to
        retire, so requiring an empty tree would leave the sensitive half of
        destroy to an operator.  The receipt is removed last: an interrupted
        traversal therefore remains retryable under the same durable token.
        If interruption lands after that last unlink, only an empty scaffold
        may be resumed without the mirror.
        """

        if receipt.status != "revoked" or receipt.resource_token != expected_token:
            raise AgentHomeError("cleanup-fenced", receipt.actor, "cleanup")
        root = Path(receipt.path)
        if root != self.agents_root / receipt.actor:
            raise AgentHomeError("receipt-mismatch", receipt.actor, "cleanup-path")
        if not root.exists() and not root.is_symlink():
            return replace(receipt, resource_token=uuid4().hex, status="cleaned")

        # Both recovery shapes delete directory entries below ``root``.  Fence
        # every ancestor before deciding whether a missing mirror represents
        # the receipt-last crash window; otherwise a symlinked ``agents``
        # directory can redirect the empty-scaffold branch outside H.
        self.safe_directory(self.hyprial_home, receipt.actor, "cleanup-home-root")
        self.safe_directory(self.agents_root, receipt.actor, "cleanup-agents-root")
        self.safe_directory(root, receipt.actor, "cleanup-agent-root", mode=0o700)

        receipt_path = root / _RECEIPT_RELATIVE
        try:
            receipt_path.lstat()
        except FileNotFoundError:
            if not self._empty_cleanup_scaffold(root, receipt.actor):
                raise AgentHomeError(
                    "cleanup-fenced", receipt.actor, "cleanup-receipt"
                ) from None
        except OSError as error:
            raise AgentHomeError(
                "cleanup-fenced", receipt.actor, "cleanup-receipt"
            ) from error
        else:
            self._validate_cleanup_topology(root, receipt.actor)
            try:
                mirrored = self._read_receipt(receipt_path, receipt.actor)
            except AgentHomeError:
                raise AgentHomeError(
                    "cleanup-fenced", receipt.actor, "cleanup-receipt"
                ) from None
            if (
                mirrored.actor != receipt.actor
                or mirrored.entity_token != receipt.entity_token
                or mirrored.resource_token != expected_token
                or mirrored.path != receipt.path
                or mirrored.owner_uid != receipt.owner_uid
                or mirrored.status not in {"ready", "revoked"}
            ):
                raise AgentHomeError(
                    "cleanup-fenced", receipt.actor, "cleanup-receipt"
                )
            try:
                for relative in _CLEANUP_DIRECTORIES:
                    directory = root / relative
                    if not directory.exists():
                        continue
                    for child in tuple(directory.iterdir()):
                        if child == receipt_path:
                            continue
                        self._remove_tree_entry(child)
                for name in _SECRET_FILES:
                    (root / name).unlink(missing_ok=True)
                receipt_path.unlink()
            except OSError as error:
                raise AgentHomeError(
                    "cleanup-not-empty", receipt.actor, "cleanup"
                ) from error

        try:
            for relative in reversed(_CLEANUP_DIRECTORIES):
                directory = root / relative
                if directory.exists():
                    directory.rmdir()
            root.rmdir()
        except OSError as error:
            raise AgentHomeError("cleanup-not-empty", receipt.actor, "cleanup") from error
        return replace(receipt, resource_token=uuid4().hex, status="cleaned")

    def _validate_cleanup_topology(self, root: Path, actor: str) -> None:
        try:
            names = {item.name for item in root.iterdir()}
            required = {item.name for item in _AGENT_SUBDIRECTORIES}
            allowed = {item.name for item in _CLEANUP_DIRECTORIES} | _SECRET_FILES
            if not required.issubset(names) or not names.issubset(allowed):
                raise AgentHomeError("cleanup-fenced", actor, "cleanup-topology")
            for relative in _CLEANUP_DIRECTORIES:
                if relative.name in names:
                    self.safe_directory(root / relative, actor, f"cleanup-{relative.name}", mode=0o700)
            for name in names & _SECRET_FILES:
                path = root / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or private_metadata_violation(path, metadata, mode=0o600):
                    raise AgentHomeError("cleanup-fenced", actor, "cleanup-secret-store")
        except OSError as error:
            raise AgentHomeError("cleanup-fenced", actor, "cleanup-topology") from error

    @staticmethod
    def _remove_tree_entry(path: Path) -> None:
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    def _empty_cleanup_scaffold(self, root: Path, actor: str) -> bool:
        """Finish only the exact empty shape left after receipt-last cleanup."""

        try:
            self.safe_directory(root, actor, "cleanup-agent-root", mode=0o700)
            allowed = {item.name for item in _CLEANUP_DIRECTORIES}
            children = tuple(root.iterdir())
            if any(child.name not in allowed for child in children):
                return False
            for child in children:
                metadata = child.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    return False
                if any(child.iterdir()):
                    return False
            return True
        except (AgentHomeError, OSError):
            return False

    @staticmethod
    def _removable(root: Path) -> bool:
        try:
            root_names = {item.name for item in root.iterdir()}
            required = {item.name for item in _AGENT_SUBDIRECTORIES}
            allowed = {item.name for item in _CLEANUP_DIRECTORIES}
            if not required.issubset(root_names) or not root_names.issubset(allowed):
                return False
            for relative in _CLEANUP_DIRECTORIES:
                path = root / relative
                if relative.name not in root_names:
                    continue
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

    def _create_home_directory(self, actor: str, path: Path) -> None:
        """Create ``H/agents/<actor>``, classifying a lost creation race.

        ``mkdir`` is the call that decides a race between two provisions of the
        same or of an aliasing name: on a case-insensitive volume the loser gets
        ``FileExistsError`` for the other spelling.  The window between the
        alias scan and this call cannot be closed without a lock spanning both,
        so the loser is classified here instead of leaking a bare ``OSError``
        out of a lane whose contract is ``AgentHomeError``.
        """

        try:
            path.mkdir(mode=0o700)
        except FileExistsError as error:
            collision = self._filesystem_alias(actor, path)
            if collision is not None and collision.name != actor:
                raise AgentHomeError("name-collision", actor, "reserve") from error
            raise AgentHomeError("unowned-residue", actor, "reserve") from error
        else:
            self._harden(path, actor, "reserve")

    def _prepare_root(self, actor: str) -> None:
        # The symlink/traversal fence covers the subtree this module manages --
        # H, H/agents and each agent directory are lstat-checked below and in
        # ``validate``.  H's own prefix is the operator's trusted filesystem
        # (e.g. macOS ``/tmp`` -> ``/private/tmp``), so it is not policed here;
        # this mirrors ``SecretResolver._validate_ancestors``, which likewise
        # starts at H rather than walking to the root.
        if self.hyprial_home.exists() or self.hyprial_home.is_symlink():
            self.safe_directory(self.hyprial_home, actor, "prepare-home-root")
        else:
            try:
                self.hyprial_home.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                # Another provision created H between the test and the call;
                # an existing H is exactly what the branch above accepts, so
                # the same fence decides whether it may be used.
                self.safe_directory(self.hyprial_home, actor, "prepare-home-root")
            else:
                self._harden(self.hyprial_home, actor, "prepare-home-root")
        if self.agents_root.exists() or self.agents_root.is_symlink():
            self.safe_directory(self.agents_root, actor, "prepare-agents-root", mode=0o700)
        else:
            try:
                self.agents_root.mkdir(mode=0o700)
            except FileExistsError:
                self.safe_directory(
                    self.agents_root, actor, "prepare-agents-root", mode=0o700
                )
            else:
                self._harden(
                    self.agents_root, actor, "prepare-agents-root", mode=0o700
                )

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
    def _harden(path: Path, actor: str, phase: str, *, mode: int = 0o700) -> None:
        """Apply this account's private access to a newly created entry."""

        try:
            harden_private_path(path, mode=mode)
        except PrivateAccessUnsupported as error:
            raise AgentHomeError("unsupported-private-acl", actor, phase) from error
        except OSError as error:
            raise AgentHomeError("io", actor, phase) from error

    @staticmethod
    def safe_directory(
        path: Path,
        actor: str,
        phase: str,
        *,
        mode: int | None = None,
    ) -> None:
        """Refuse ``path`` unless it is a real directory private to this account.

        Public because the user home (``hyprial.users.home``) holds its
        directories to the same contract: no symlink, junction or other
        reparse point, no non-directory, and private access.  On POSIX that is
        the current uid and exactly ``mode`` when one is given.  On Windows it
        is the owner/DACL boundary implemented by
        :mod:`hyprial.kernel.impl.platform.privacy.private_access`; a platform that
        cannot inspect that boundary refuses rather than skipping it.  Raises
        :class:`AgentHomeError` naming ``phase``; nothing here is
        agent-specific.
        """

        try:
            metadata = path.lstat()
        except OSError as error:
            raise AgentHomeError("io", actor, phase) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgentHomeError("unsafe-path", actor, phase)
        try:
            violation = private_metadata_violation(path, metadata, mode=mode)
        except OSError as error:
            raise AgentHomeError("io", actor, phase) from error
        if violation is not None:
            raise AgentHomeError(violation, actor, phase)

    @staticmethod
    def _read_receipt(path: Path, actor: str) -> HomeReceipt:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise AgentHomeError("unsafe-path", actor, "read-receipt")
            violation = private_metadata_violation(path, metadata, mode=0o600)
            if violation is not None:
                raise AgentHomeError(violation, actor, "read-receipt")
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
