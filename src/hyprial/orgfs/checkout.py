"""Read-only materialized checkout for an orgfs space.

The checkout is a projection only: filesystem changes are never observed or
written back to orgfs.  Callers drive it from the public watch stream by
passing :class:`ChangeEvent` objects to :meth:`CheckoutManager.apply`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .api import ChangeEvent, NodeInfo


ORGFS_CHECKOUT_STATUS_FILE: Final[str] = ".orgfs-checkout.json"

_WINDOWS_INVALID = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class _ProjectedNode:
    info: NodeInfo
    path: Path
    uses_unsafe_name_placeholder: bool


class CheckoutManager:
    """Maintain one read-only checkout using injected orgfs dependencies."""

    def __init__(
        self, facade: Any, blobs: Any, space_id: str, root: Path | str
    ) -> None:
        self.facade = facade
        self.blobs = blobs
        self.space_id = space_id
        requested_root = Path(os.path.abspath(os.fspath(root)))
        if requested_root.is_symlink():
            raise ValueError("checkout root must not be a symbolic link")
        self.root = Path(os.path.realpath(requested_root))
        self._root_real = self.root
        self._lock = threading.RLock()
        self._paths_by_node: dict[str, Path] = {}
        self._unmaterialized: dict[str, dict[str, str]] = {}

    def materialize(self) -> None:
        """Rebuild the complete projection and remove stale checkout entries."""

        with self._lock:
            self._prepare_root()
            projected = self._scan_tree()
            expected = {item.path for item in projected.values()}
            expected.add(self.root / ORGFS_CHECKOUT_STATUS_FILE)
            self._remove_stale(expected)
            self._unmaterialized.clear()
            for item in sorted(
                projected.values(),
                key=lambda value: (len(value.path.parts), value.path),
            ):
                self._materialize_node(item)
            self._paths_by_node = {
                node_id: item.path for node_id, item in projected.items()
            }
            self._write_status()
            self._make_read_only()

    def reconcile(self) -> None:
        """Fully reconcile the projection, including unavailable blob retries."""

        self.materialize()

    def apply(self, event: ChangeEvent) -> None:
        """Apply one watch event, re-reading authoritative node state."""

        self.apply_change(event.kind, event.node.node_id)

    def apply_change(self, kind: str, changed_node_id: str) -> None:
        """Apply a frozen watch projection delivered by CheckoutAuthority."""

        with self._lock:
            self._prepare_root()
            projected = self._scan_tree()
            projected_paths = {item.path for item in projected.values()}
            changed_paths = {
                node_id
                for node_id in self._paths_by_node.keys() | projected.keys()
                if self._paths_by_node.get(node_id)
                != (projected[node_id].path if node_id in projected else None)
            }
            if kind in {"removed", "moved"}:
                changed_paths.add(changed_node_id)
            for node_id in changed_paths:
                previous = self._paths_by_node.get(node_id)
                if previous is not None:
                    self._remove_path(previous)
                    self._remove_empty_parents(previous.parent, projected_paths)

            affected = (
                changed_paths
                | (self._unmaterialized.keys() & projected.keys())
                | {
                    node_id
                    for node_id, item in projected.items()
                    if node_id == changed_node_id
                    or self._is_below(item.path, projected.get(changed_node_id))
                }
            )
            for node_id in affected:
                self._unmaterialized.pop(node_id, None)
            for node_id in sorted(
                (value for value in affected if value in projected),
                key=lambda value: (
                    len(projected[value].path.parts),
                    projected[value].path,
                ),
            ):
                self._materialize_node(projected[node_id])

            self._paths_by_node = {
                node_id: item.path for node_id, item in projected.items()
            }
            self._write_status()
            self._make_read_only()

    def disable(self) -> None:
        """Remove the checkout tree without following checkout symlinks."""

        with self._lock:
            if not self.root.exists() and not self.root.is_symlink():
                return
            if self.root.is_symlink():
                self.root.unlink()
            else:
                self._make_writable_tree(self.root)
                shutil.rmtree(self.root)
            self._paths_by_node.clear()
            self._unmaterialized.clear()

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (
            self.root.is_symlink()
            or Path(os.path.realpath(self.root)) != self._root_real
        ):
            raise ValueError("checkout root changed identity")
        self.root.chmod(0o755)

    def _scan_tree(self) -> dict[str, _ProjectedNode]:
        projected: dict[str, _ProjectedNode] = {}

        def visit(parent_ref: str, parent_path: Path) -> None:
            for info in self.facade.listdir(self.space_id, parent_ref):
                name, uses_unsafe_name_placeholder = self._projected_name(info)
                path = parent_path / name
                projected[info.node_id] = _ProjectedNode(
                    info, path, uses_unsafe_name_placeholder
                )
                if info.kind == "dir":
                    visit(f"id:{info.node_id}", path)

        visit("id:root", self.root)
        return projected

    def _projected_name(self, info: NodeInfo) -> tuple[str, bool]:
        if not self._valid_name(info.name) or info.name == ORGFS_CHECKOUT_STATUS_FILE:
            return f"orgfs-quarantine-{self._node_suffix(info.node_id)}", True
        if info.name_conflict:
            return f"{info.name}.conflict-{self._node_suffix(info.node_id)}", False
        return info.name, False

    @staticmethod
    def _node_suffix(node_id: str) -> str:
        suffix = "".join(character for character in node_id[:8] if character.isalnum())
        return suffix or "unknown"

    @staticmethod
    def _valid_name(name: str) -> bool:
        if not name or name in {".", ".."}:
            return False
        if any(
            ord(character) < 32 or character in _WINDOWS_INVALID for character in name
        ):
            return False
        if name[-1:] in {" ", "."}:
            return False
        stem = name.split(".", 1)[0].upper()
        return stem not in _WINDOWS_RESERVED

    def _safe_target(self, path: Path) -> Path | None:
        candidate = Path(os.path.abspath(path))
        resolved = Path(os.path.realpath(candidate))
        try:
            candidate.relative_to(self.root)
            resolved.relative_to(self._root_real)
        except ValueError:
            return None
        # A different real path means some existing component is a symlink.
        # Do not follow it even when its target happens to remain under root.
        if candidate != resolved:
            return None
        return candidate

    def _materialize_node(self, item: _ProjectedNode) -> None:
        target = self._safe_target(item.path)
        if target is None:
            self._record(item.info, item.path, "unsafe-path")
            return
        if item.uses_unsafe_name_placeholder:
            self._record(item.info, target, "unsafe-name-placeholder")
        if item.info.kind == "dir":
            if target.exists() and not target.is_dir():
                self._remove_path(target)
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
            return
        if item.info.kind == "blob":
            try:
                if self.blobs is None or not item.info.blob_hash:
                    raise FileNotFoundError(item.info.blob_hash or "missing digest")
                content = self.blobs.get(self.space_id, item.info.blob_hash)
            except Exception:
                self._remove_path(target)
                self._record(item.info, target, "blob-unavailable")
                return
        else:
            content = self.facade.read_bytes(self.space_id, f"id:{item.info.node_id}")
        self._atomic_write(target, bytes(content))

    def _atomic_write(self, target: Path, content: bytes) -> None:
        safe = self._safe_target(target)
        if safe is None:
            raise ValueError("unsafe checkout target")
        parent = safe.parent
        if self._safe_target(parent) is None:
            raise ValueError("unsafe checkout parent")
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o755)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{safe.name}.", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o444)
            os.replace(temporary, safe)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, info: NodeInfo, path: Path, reason: str) -> None:
        try:
            shown_path = path.relative_to(self.root).as_posix()
        except ValueError:
            shown_path = path.name
        self._unmaterialized[info.node_id] = {
            "nodeId": info.node_id,
            "path": shown_path,
            "reason": reason,
        }

    def _write_status(self) -> None:
        value = {
            "unmaterialized": sorted(
                self._unmaterialized.values(), key=lambda item: item["nodeId"]
            ),
            "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        status_path = self.root / ORGFS_CHECKOUT_STATUS_FILE
        if status_path.is_symlink():
            status_path.unlink()
        self._atomic_write(status_path, payload)

    def _remove_stale(self, expected: set[Path]) -> None:
        def prune(directory: Path) -> None:
            for entry in tuple(directory.iterdir()):
                if entry == self.root / ORGFS_CHECKOUT_STATUS_FILE:
                    continue
                descendants = any(path.is_relative_to(entry) for path in expected)
                if entry not in expected and not descendants:
                    self._remove_path(entry)
                elif entry.is_dir() and not entry.is_symlink():
                    entry.chmod(0o755)
                    prune(entry)

        prune(self.root)

    def _remove_path(self, path: Path) -> None:
        safe = self._safe_target(path)
        if safe is None:
            return
        try:
            mode = safe.lstat().st_mode
        except FileNotFoundError:
            return
        parent = safe.parent
        if parent.exists() and not parent.is_symlink():
            parent.chmod(0o755)
        if stat.S_ISDIR(mode):
            self._make_writable_tree(safe)
            shutil.rmtree(safe)
        else:
            safe.unlink()

    def _remove_empty_parents(self, path: Path, projected_paths: set[Path]) -> None:
        while path != self.root:
            safe = self._safe_target(path)
            if safe is None or not safe.is_dir():
                return
            if safe in projected_paths:
                return
            safe.chmod(0o755)
            try:
                safe.rmdir()
            except OSError:
                return
            path = safe.parent

    @staticmethod
    def _is_below(path: Path, parent: _ProjectedNode | None) -> bool:
        return (
            parent is not None
            and path != parent.path
            and path.is_relative_to(parent.path)
        )

    def _make_read_only(self) -> None:
        if not self.root.exists():
            return
        for current, directories, files in os.walk(self.root, followlinks=False):
            current_path = Path(current)
            for name in files:
                path = current_path / name
                if not path.is_symlink():
                    path.chmod(0o444)
            for name in directories:
                path = current_path / name
                if not path.is_symlink():
                    path.chmod(0o555)
        self.root.chmod(0o555)

    @staticmethod
    def _make_writable_tree(root: Path) -> None:
        for current, directories, files in os.walk(
            root, topdown=False, followlinks=False
        ):
            current_path = Path(current)
            for name in files:
                path = current_path / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = current_path / name
                if not path.is_symlink():
                    path.chmod(0o700)
            current_path.chmod(0o700)
