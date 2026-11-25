# Decision record (OC4 J22 local owner storage; replaces PersonalOrgOwnerStore)
# - Owner-scoped records belong only on the local machine under
#   <HYPRIAL_HOME>/state/owner-store/<userKey>/, never in an OrgFS personal org.
# - LocalOwnerStore reuses LocalOwnerStore CAS mechanics, symlink fail-closed
#   ancestry validation, and private home/mode enforcement from
#   provider_keys.storage.local so atomic compare-and-set is never duplicated.
# - OwnerScope.stat and OwnerScope.listdir populate NodeInfo.version with the
#   current CAS version token for document nodes and fail closed when unreadable.
"""Local per-owner storage port and ``<HYPRIAL_HOME>/state/`` adapter."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Protocol, runtime_checkable

from hyprial.daemon.impl.orgfs.api import NodeInfo, OrgFsError
from hyprial.daemon.impl.provider_keys.storage.local import (
    LocalOwnerStore as _CasStore,
    ensure_private_dir_chain,
    ensure_private_directory_entry,
    require_private_home,
    validate_no_symlink_descendant,
)
from hyprial.daemon.impl.provider_keys.storage.port import StaleWrite
from hyprial.identity import OrgMembership, ProviderKeys
from hyprial.kernel import path_is_reparse

OWNER_SCOPE_LOCAL = "local-state"

__all__ = [
    "OWNER_SCOPE_LOCAL",
    "LocalOwnerStore",
    "OwnerScope",
    "OwnerStorePort",
    "OwnerStoreUnavailable",
]


class OwnerStoreUnavailable(RuntimeError):
    """The owner's store cannot be used yet; ``reason`` says why."""

    def __init__(self, owner: str, reason: str, *, state: str) -> None:
        super().__init__(f"owner store for {owner} is {state}: {reason}")
        self.owner = owner
        self.reason = reason
        self.state = state


def _relative(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("owner-store paths are non-empty and relative")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("owner-store paths may not contain empty, '.' or '..' segments")
    return path


def _store_path(path: str) -> str:
    relative = _relative(path)
    for prefix in ("directory/provider-keys", "directory/provider-grants"):
        if relative == prefix or relative.startswith(f"{prefix}/"):
            return relative
    return f"directory/provider-keys/{relative}"


def _orgfs_unavailable(path: str) -> OrgFsError:
    return OrgFsError("unavailable", {"path": path})


@dataclass(frozen=True, slots=True)
class OwnerScope:
    """One owner's local state directory; every path is relative to it."""

    owner: str
    space_id: str
    kind: str
    root: Path

    def _canonical_root(self) -> Path:
        try:
            return require_private_home(self.root, lambda reason: _orgfs_unavailable(str(self.root)))
        except OrgFsError:
            raise

    def _cas(self) -> _CasStore:
        return _CasStore(self._canonical_root())

    def _fs_path(self, path: str) -> tuple[Path, Path, tuple[str, ...]]:
        canonical_root = self._canonical_root()
        store_rel = _store_path(path)
        parts = tuple(store_rel.split("/"))
        return canonical_root, canonical_root.joinpath(*parts), parts

    def _node(self, target: Path, rel: str, kind: str) -> NodeInfo:
        version = ""
        size: int | None = None
        if kind == "doc":
            try:
                found_version = self._cas().version_of(_store_path(rel))
            except ProviderKeys.Error:
                raise _orgfs_unavailable(rel) from None
            if not found_version:
                raise _orgfs_unavailable(rel)
            version = found_version
            try:
                observed = target.lstat()
            except OSError:
                raise _orgfs_unavailable(rel) from None
            if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed) or not stat.S_ISREG(observed.st_mode):
                raise _orgfs_unavailable(rel)
            size = observed.st_size
        return NodeInfo(
            space_id=self.space_id,
            node_id=rel,
            kind=kind,
            name=target.name,
            path=rel,
            size=size,
            blob_hash=None,
            doc_id=None,
            version=version,
            modified_by=self.owner,
            modified_via="local",
            name_conflict=False,
            deleted=False,
            uri=f"{self.kind}:{self.owner}:{self.space_id}:{rel}",
        )

    def stat(self, path: str) -> NodeInfo:
        rel = _relative(path)
        canonical_root, target, parts = self._fs_path(path)
        resolved = validate_no_symlink_descendant(
            canonical_root, parts, lambda _reason: _orgfs_unavailable(path)
        )
        if resolved is None:
            raise OrgFsError("unknown-doc", {"path": path})
        try:
            observed = resolved.lstat()
        except OSError:
            raise _orgfs_unavailable(path) from None
        if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
            raise _orgfs_unavailable(path)
        kind = "dir" if stat.S_ISDIR(observed.st_mode) else "doc"
        return self._node(resolved, rel, kind)

    def listdir(self, path: str) -> tuple[NodeInfo, ...]:
        rel = _relative(path).rstrip("/")
        canonical_root, _, parts = self._fs_path(path)
        resolved = validate_no_symlink_descendant(
            canonical_root, parts, lambda _reason: _orgfs_unavailable(path), expect_leaf_dir=True
        )
        if resolved is None:
            raise OrgFsError("unknown-doc", {"path": path})
        nodes: list[NodeInfo] = []
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: p.name)
        except OSError:
            raise _orgfs_unavailable(path) from None
        for entry in entries:
            if entry.name.startswith("."):
                continue
            child_rel = f"{rel}/{entry.name}"
            try:
                observed = entry.lstat()
            except OSError:
                raise _orgfs_unavailable(child_rel) from None
            if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
                raise _orgfs_unavailable(child_rel)
            if stat.S_ISDIR(observed.st_mode):
                ensure_private_directory_entry(
                    entry,
                    observed,
                    lambda _reason, target_rel=child_rel: _orgfs_unavailable(target_rel),
                )
            child_kind = "dir" if stat.S_ISDIR(observed.st_mode) else "doc"
            nodes.append(self._node(entry, child_rel, child_kind))
        return tuple(nodes)

    def mkdir(self, path: str) -> NodeInfo:
        rel = _relative(path)
        canonical_root, target, parts = self._fs_path(path)
        validate_no_symlink_descendant(
            canonical_root, parts, lambda _reason: _orgfs_unavailable(path), expect_leaf_dir=True
        )
        resolved = ensure_private_dir_chain(
            canonical_root, target, lambda _reason: _orgfs_unavailable(path), create=True
        )
        assert resolved is not None
        return self._node(resolved, rel, "dir")

    def read_text(self, path: str) -> tuple[str, str]:
        store_rel = _store_path(path)
        try:
            found = self._cas().read(store_rel)
        except ProviderKeys.Error:
            raise _orgfs_unavailable(path) from None
        if found is None:
            raise OrgFsError("unknown-doc", {"path": path})
        return found

    def write_text(
        self,
        path: str,
        content: str,
        *,
        expect_version: str | None = None,
        create_only: bool = False,
    ) -> str:
        """Conditional write; returns the new document version."""
        store_rel = _store_path(path)
        try:
            return self._cas().write(
                store_rel,
                content,
                expect_version=expect_version,
                create_only=create_only,
            )
        except StaleWrite:
            raise OrgFsError("stale-write", {"path": path}) from None
        except ProviderKeys.Error:
            raise _orgfs_unavailable(path) from None


@runtime_checkable
class OwnerStorePort(Protocol):
    """Where an owner's protected records live; adapters are swappable."""

    def open(self, owner: str) -> OwnerScope: ...


class LocalOwnerStore:
    """The local ``<HYPRIAL_HOME>/state/`` owner-store adapter."""

    def __init__(self, home: Path | str) -> None:
        self._home = Path(os.fspath(home)) if isinstance(home, os.PathLike) else Path(home) if isinstance(home, str) else home

    def open(self, owner: str) -> OwnerScope:
        try:
            key = OrgMembership.user_key(owner)
        except OrgMembership.OrgMembershipError:
            raise OwnerStoreUnavailable(owner, "owner-invalid", state="unavailable") from None
        canonical_home = require_private_home(
            self._home,
            lambda reason: OwnerStoreUnavailable(owner, reason, state="unavailable"),
        )
        base = canonical_home / "state" / "owner-store"
        owner_root = base / key
        canonical_root = ensure_private_dir_chain(
            canonical_home,
            owner_root,
            lambda reason: OwnerStoreUnavailable(owner, reason, state="unavailable"),
            create=True,
        )
        assert canonical_root is not None
        bare = owner.removeprefix("user:").strip()
        return OwnerScope(bare, f"local:{key}", OWNER_SCOPE_LOCAL, canonical_root)
