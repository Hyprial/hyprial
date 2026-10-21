"""Crash-resumable, local-only removal of one OrgFS space replica."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Mapping

from hyprial.daemon.impl.orgfs.api import OrgFsError, SpaceInfo


_SAFE_SPACE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int


@dataclass(slots=True)
class _DeletionFence:
    component_names: tuple[str, ...]
    component_identities: tuple[_DirectoryIdentity, ...]
    descriptors: tuple[int, ...]
    target_name: str
    target_identity: _DirectoryIdentity | None

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors = ()


@dataclass(frozen=True, slots=True)
class _SpacePurgeIntent:
    state: str
    cleanup_digests: tuple[str, ...] = ()


class RuntimeSpacePurge:
    """Detach every live owner before deleting one fenced local space tree."""

    def _intent_directory(self) -> Path:
        return self.state_dir / "orgfs" / "purge-intents"

    def _intent_path(self, space_id: str) -> Path:
        self._validate_space_id(space_id)
        return self._intent_directory() / f"{space_id}.json"

    @staticmethod
    def _validate_space_id(space_id: str) -> None:
        if (
            not isinstance(space_id, str)
            or not space_id
            or space_id in {".", ".."}
            or _SAFE_SPACE_ID.fullmatch(space_id) is None
        ):
            raise OrgFsError(
                "unsafe-space-path",
                {"spaceId": str(space_id), "reason": "invalid-space-id"},
            )

    def _fenced_space_path(self, root: Path, space_id: str) -> Path:
        self._validate_space_id(space_id)
        return root / space_id

    def _space_storage_paths(self, space_id: str) -> tuple[Path, Path]:
        return (
            self._fenced_space_path(
                self.state_dir / "orgfs" / "spaces", space_id
            ),
            self._fenced_space_path(
                self.state_dir / "orgfs" / "replica", space_id
            ),
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_space_purge_intent(
        self,
        space_id: str,
        state: str,
        cleanup_digests: tuple[str, ...] = (),
    ) -> None:
        directory = self._intent_directory()
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{space_id}.", dir=directory
        )
        temporary = Path(temporary_name)
        payload = json.dumps(
            {
                "schemaVersion": 2,
                "spaceId": space_id,
                "state": state,
                "cleanupDigests": list(cleanup_digests),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._intent_path(space_id))
            self._fsync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_space_purge_intent(
        self, space_id: str
    ) -> _SpacePurgeIntent | None:
        try:
            value = json.loads(self._intent_path(space_id).read_text("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, OrgFsError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("spaceId") != space_id
            or value.get("state") not in {"prepared", "detached"}
        ):
            return None
        schema_version = value.get("schemaVersion")
        if schema_version == 1:
            return _SpacePurgeIntent(str(value["state"]))
        cleanup_digests = value.get("cleanupDigests")
        if (
            schema_version != 2
            or not isinstance(cleanup_digests, list)
            or any(
                not isinstance(digest, str)
                or _SAFE_DIGEST.fullmatch(digest) is None
                for digest in cleanup_digests
            )
        ):
            return None
        return _SpacePurgeIntent(
            str(value["state"]), tuple(sorted(set(cleanup_digests)))
        )

    def _pending_space_purge_ids(self) -> tuple[str, ...]:
        directory = self._intent_directory()
        if not directory.is_dir() or directory.is_symlink():
            return ()
        pending: list[str] = []
        for path in sorted(directory.glob("*.json")):
            space_id = path.stem
            if self._read_space_purge_intent(space_id) is not None:
                pending.append(space_id)
        return tuple(pending)

    def _space_info(self, space_id: str) -> SpaceInfo | None:
        return next(
            (item for item in self.facade.spaces() if item.space_id == space_id),
            None,
        )

    def _record_deleted_org(self, org: str) -> None:
        self._validate_space_id(org)
        directory = self.state_dir / "orgfs" / "deleted-orgs"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{org}.json"
        if target.is_symlink():
            raise OrgFsError(
                "unsafe-space-path", {"spaceId": org, "reason": "symlink"}
            )
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{org}.", dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(
                    json.dumps(
                        {"schemaVersion": 1, "org": org, "state": "deleted"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            self._fsync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _deleted_org_state(self, org: str) -> str | None:
        try:
            self._validate_space_id(org)
            target = self.state_dir / "orgfs" / "deleted-orgs" / f"{org}.json"
            if target.is_symlink():
                return None
            value = json.loads(target.read_text("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, OrgFsError):
            return None
        if (
            isinstance(value, dict)
            and value.get("schemaVersion") == 1
            and value.get("org") == org
            and value.get("state") == "deleted"
        ):
            return "deleted"
        return None

    def _validate_space_purge_policy(self, space_id: str, info: SpaceInfo) -> None:
        if info.owner != self.author:
            raise OrgFsError(
                "space-purge-refused",
                {"spaceId": space_id, "reason": "not-local-owner"},
            )
        other_members = sorted(
            member.user
            for member in self.facade.members(space_id)
            if member.user != info.owner
        )
        if other_members:
            raise OrgFsError(
                "space-purge-refused",
                {
                    "spaceId": space_id,
                    "reason": "other-members",
                    "members": other_members,
                },
            )
        binding = self._space_binding_state(space_id)
        if binding is None:
            return
        if not isinstance(binding, Mapping):
            binding = {"org": "unknown", "state": "unknown"}
        org = str(binding.get("org", "unknown"))
        state = str(binding.get("state", "unknown"))
        if state != "deleted":
            raise OrgFsError(
                "space-purge-refused",
                {
                    "spaceId": space_id,
                    "org": org,
                    "state": state,
                    "reason": "org-not-deleted",
                },
            )
        self._record_deleted_org(org)

    def _wait_for_space_lifecycle(self, space_id: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lifecycle_lock:
                authority = self._lifecycle_authority
                active = (
                    ()
                    if authority is None
                    else tuple(authority.commands.values())
                )
            if not any(
                getattr(command.operation, "space_id", None) == space_id
                for command in active
            ):
                return
            time.sleep(0.005)
        raise TimeoutError("orgfs space lifecycle did not detach")

    def _detach_space(self, space_id: str) -> None:
        self._wait_for_space_lifecycle(space_id)
        with self._checkout_lock:
            checkout = self._checkouts.pop(space_id, None)
        if checkout is not None and not checkout.close(timeout=5.0, remove_tree=True):
            raise TimeoutError("orgfs checkout did not detach")
        self._write_checkout_config()

        with self._resource_lock:
            creation = self._mesh_creations.get(space_id)
        if creation is not None and not creation.done.wait(5.0):
            raise TimeoutError("orgfs mesh creation did not detach")
        if not self.facade.detach_space(space_id, timeout=5.0):
            raise TimeoutError("orgfs facade did not detach")
        with self._resource_lock:
            mesh = self._meshes.pop(space_id, None)
            replica = self._replicas.pop(space_id, None)
            self._replica_creation_locks.pop(space_id, None)
        if mesh is not None:
            mesh.close(timeout=5.0)
        if replica is not None and not replica.close(timeout=5.0):
            raise TimeoutError("orgfs replica did not detach")

        with self._supplier_device_lock:
            watch = self._supplier_device_watches.pop(space_id, None)
            self._supplier_device_owners.pop(space_id, None)
        if watch is not None:
            close = getattr(watch, "close", None)
            if callable(close):
                close()
        with self._pending_announces_lock:
            self._holders.pop(space_id, None)
            self._known_holders.pop(space_id, None)
            self._edge_skipped_holdings = {
                item for item in self._edge_skipped_holdings if item[0] != space_id
            }

        store = self.stores.detach(space_id)
        if store is not None:
            store.close()
        self._announce_all()

    @staticmethod
    def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
        return _DirectoryIdentity(value.st_dev, value.st_ino)

    def _open_deletion_fence(self, path: Path, space_id: str) -> _DeletionFence:
        parent = Path(os.path.abspath(path.parent))
        descriptors: list[int] = []
        component_names = tuple(parent.parts[1:])
        identities: list[_DirectoryIdentity] = []
        try:
            descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
            descriptors.append(descriptor)
            identities.append(self._directory_identity(os.fstat(descriptor)))
            for index, component in enumerate(component_names):
                try:
                    child = os.open(
                        component, _DIRECTORY_FLAGS, dir_fd=descriptor
                    )
                except FileNotFoundError:
                    if index != len(component_names) - 1:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(
                        component, _DIRECTORY_FLAGS, dir_fd=descriptor
                    )
                descriptors.append(child)
                descriptor = child
                identities.append(self._directory_identity(os.fstat(child)))
            try:
                target = os.stat(
                    path.name, dir_fd=descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                target_identity = None
            else:
                if stat.S_ISLNK(target.st_mode) or not stat.S_ISDIR(target.st_mode):
                    raise OSError("purge target is not a directory")
                target_identity = self._directory_identity(target)
        except OSError as error:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise OrgFsError(
                "unsafe-space-path",
                {"spaceId": space_id, "reason": "unsafe-ancestry"},
            ) from error
        return _DeletionFence(
            component_names,
            tuple(identities),
            tuple(descriptors),
            path.name,
            target_identity,
        )

    def _space_storage_fences(
        self, space_id: str
    ) -> tuple[_DeletionFence, _DeletionFence]:
        fences: list[_DeletionFence] = []
        try:
            for path in self._space_storage_paths(space_id):
                fences.append(self._open_deletion_fence(path, space_id))
        except Exception:
            for fence in reversed(fences):
                fence.close()
            raise
        return fences[0], fences[1]

    def _validate_space_storage_paths(self, space_id: str) -> None:
        fences = self._space_storage_fences(space_id)
        for fence in reversed(fences):
            fence.close()

    def _verified_deletion_target(
        self, fence: _DeletionFence, space_id: str
    ) -> tuple[int, int] | None:
        descriptor = os.dup(fence.descriptors[0])
        try:
            if (
                self._directory_identity(os.fstat(descriptor))
                != fence.component_identities[0]
            ):
                raise OSError("filesystem root identity changed")
            for component, expected in zip(
                fence.component_names, fence.component_identities[1:], strict=True
            ):
                child = os.open(
                    component, _DIRECTORY_FLAGS, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
                if self._directory_identity(os.fstat(child)) != expected:
                    raise OSError("directory identity changed")
            try:
                target = os.stat(
                    fence.target_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if fence.target_identity is None:
                    os.close(descriptor)
                    return None
                raise OSError("purge target disappeared") from None
            if (
                fence.target_identity is None
                or stat.S_ISLNK(target.st_mode)
                or not stat.S_ISDIR(target.st_mode)
                or self._directory_identity(target) != fence.target_identity
            ):
                raise OSError("purge target identity changed")
            target_descriptor = os.open(
                fence.target_name, _DIRECTORY_FLAGS, dir_fd=descriptor
            )
            if (
                self._directory_identity(os.fstat(target_descriptor))
                != fence.target_identity
            ):
                os.close(target_descriptor)
                raise OSError("opened purge target identity changed")
            return descriptor, target_descriptor
        except OSError as error:
            os.close(descriptor)
            raise OrgFsError(
                "unsafe-space-path",
                {"spaceId": space_id, "reason": "path-identity-changed"},
            ) from error

    def _remove_directory_contents(self, descriptor: int) -> None:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                value = os.stat(
                    entry.name, dir_fd=descriptor, follow_symlinks=False
                )
                if stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode):
                    child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        identity = self._directory_identity(os.fstat(child))
                        if identity != self._directory_identity(value):
                            raise OSError("child directory identity changed")
                        self._remove_directory_contents(child)
                        current = os.stat(
                            entry.name, dir_fd=descriptor, follow_symlinks=False
                        )
                        if self._directory_identity(current) != identity:
                            raise OSError("child directory was replaced")
                    finally:
                        os.close(child)
                    os.rmdir(entry.name, dir_fd=descriptor)
                else:
                    os.unlink(entry.name, dir_fd=descriptor)
        os.fsync(descriptor)

    def _remove_fenced_tree(
        self, fence: _DeletionFence, space_id: str
    ) -> None:
        verified = self._verified_deletion_target(fence, space_id)
        if verified is None:
            return
        parent_descriptor, target_descriptor = verified
        try:
            try:
                self._remove_directory_contents(target_descriptor)
                current = os.stat(
                    fence.target_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if self._directory_identity(current) != fence.target_identity:
                    raise OSError("purge target was replaced")
                os.rmdir(fence.target_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except OSError as error:
                raise OrgFsError(
                    "unsafe-space-path",
                    {"spaceId": space_id, "reason": "deletion-race"},
                ) from error
        finally:
            os.close(target_descriptor)
            os.close(parent_descriptor)

    def _delete_space_storage(
        self,
        space_id: str,
        fences: tuple[_DeletionFence, _DeletionFence],
        digests: tuple[str, ...],
    ) -> None:
        for fence in fences:
            self._remove_fenced_tree(fence, space_id)
        for digest in digests:
            self.blobs.release(space_id, digest)
            self.blobs.delete_if_unreferenced(digest)
        self.blobs.unregister_purge_projection(space_id)

    def _space_purge_readback(self, space_id: str) -> None:
        space_path, replica_path = self._space_storage_paths(space_id)
        with self._resource_lock:
            registered = (
                space_id in self._meshes
                or space_id in self._replicas
                or space_id in self._mesh_creations
            )
        with self._checkout_lock:
            registered = registered or space_id in self._checkouts
        if (
            self._space_info(space_id) is not None
            or space_id in self.stores
            or registered
            or space_path.exists()
            or replica_path.exists()
        ):
            raise RuntimeError("orgfs space purge readback found local residue")

    def _finish_space_purge(
        self, space_id: str, intent_state: _SpacePurgeIntent
    ) -> None:
        with self._resource_lock:
            self._purging_spaces.add(space_id)
        fences = self._space_storage_fences(space_id)
        try:
            if intent_state.state != "detached":
                self._detach_space(space_id)
            current_digests = {
                digest
                for _space, digest, _reason in self.blobs.references(space_id)
            }
            digests = tuple(
                sorted(current_digests | set(intent_state.cleanup_digests))
            )
            self._write_space_purge_intent(space_id, "detached", digests)
            self._delete_space_storage(space_id, fences, digests)
        finally:
            for fence in reversed(fences):
                fence.close()
        self._space_purge_readback(space_id)
        intent = self._intent_path(space_id)
        intent.unlink(missing_ok=True)
        if intent.parent.is_dir():
            self._fsync_directory(intent.parent)

    def _resume_space_purges(self) -> None:
        for space_id in self._pending_space_purge_ids():
            try:
                intent_state = self._read_space_purge_intent(space_id)
                if intent_state is not None:
                    self._finish_space_purge(space_id, intent_state)
            except Exception as error:
                if self.logger is not None:
                    self.logger(
                        "error",
                        "orgfs.space-purge.resume-failed",
                        spaceId=space_id,
                        detail=str(error),
                    )

    def purge_space(self, space_id: str) -> dict[str, object]:
        """Remove one local replica without emitting a replicated delete."""

        self._validate_space_id(space_id)
        intent_state = self._read_space_purge_intent(space_id)
        if intent_state is not None:
            try:
                self._finish_space_purge(space_id, intent_state)
            except OrgFsError:
                raise
            except Exception as error:
                raise OrgFsError(
                    "space-purge-incomplete",
                    {"spaceId": space_id, "resumable": True, "message": str(error)},
                ) from error
            return {"spaceId": space_id, "purged": True, "localOnly": True}
        info = self._space_info(space_id)
        if info is None:
            return {"spaceId": space_id, "purged": False, "localOnly": True}
        self._validate_space_storage_paths(space_id)
        self._validate_space_purge_policy(space_id, info)
        intent_state = _SpacePurgeIntent("prepared")
        self._write_space_purge_intent(space_id, intent_state.state)
        try:
            self._finish_space_purge(space_id, intent_state)
        except OrgFsError:
            raise
        except Exception as error:
            raise OrgFsError(
                "space-purge-incomplete",
                {"spaceId": space_id, "resumable": True, "message": str(error)},
            ) from error
        return {"spaceId": space_id, "purged": True, "localOnly": True}
