# Decision record (file header; no inline comments by repository rule):
# - OC5 per-(agent, org) exclusion store and dynamic org label derivation.
# - Persists [{agentId, directorySpace}] only on the agent's local machine at
#   <HYPRIAL_HOME>/state/org-agent-exclusions.json with 0o600 permissions.
# - Read-modify-write cycles serialize intra-process via per-path RLock and
#   inter-process via exclusive OS locks on a verified sibling lock file.
# - Every read, mutation, lock access, and carried payload read routes through
#   one fail-closed private-metadata validator checking non-system ancestry,
#   no symlink/reparse points, regular/directory type, st_nlink == 1, and
#   private owner/mode (0o700 directories, 0o600 files), plus post-open fstat
#   descriptor verification on O_NOFOLLOW opens.
# - adopt_for_agent computes the added delta inside the locked transaction and
#   returns an OrgAgentExclusionAdoption undo handle (and attaches it via
#   on_committed / committed_adoption even if post-publish dir fsync fails).
# - Membership labels are derived dynamically from the owner's confirmed active
#   orgs minus local exclusions, for both ordinary agents and user agents.
# - Exclusions only gate member directory publication / label visibility; they
#   never block transport, inbound, delivery or OrgFS sync (A2).
"""Per-(agent, org) exclusions and dynamic org label derivation (OC5)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any, Literal

from hyprial.daemon.impl.org.membership.context import for_app
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import directory_owner_principal
from hyprial.kernel import (
    DaemonRequestError,
    PrivateAccessUnsupported,
    fsync_directory,
    harden_private_path,
    is_agent_id_segment,
    lock_exclusive,
    open_private_file,
    path_is_reparse,
    private_metadata_violation,
    secure_private_directory,
    unlock,
)

ORG_AGENT_EXCLUSIONS_FILENAME = "org-agent-exclusions.json"
TRANSFER_EXCLUSIONS_RELPATH = f"state/{ORG_AGENT_EXCLUSIONS_FILENAME}"
_LOCK_TIMEOUT_SECONDS = 15.0


class OrgAgentExclusionStorageError(OSError):
    """Raised when local or carried org-agent-exclusions storage fails safety or I/O checks."""

    def __init__(
        self,
        message: str,
        *,
        committed_adoption: OrgAgentExclusionAdoption | None = None,
    ) -> None:
        super().__init__(message)
        self.committed_adoption = committed_adoption


@dataclass(frozen=True, slots=True)
class OrgAgentExclusionAdoption:
    """Exact committed delta and undo handle from one locked adoption transaction."""

    store: OrgAgentExclusionStore
    agent_id: str
    added_spaces: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added_spaces)

    def __bool__(self) -> bool:
        return self.changed

    def undo(self) -> bool:
        if not self.added_spaces:
            return False
        return self.store.remove_for_agent(self.agent_id, self.added_spaces)


@dataclass(slots=True)
class _PathLockEntry:
    rlock: threading.RLock
    depth: int = 0
    lock_fd: int | None = None


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, _PathLockEntry] = {}

__all__ = [
    "ORG_AGENT_EXCLUSIONS_FILENAME",
    "TRANSFER_EXCLUSIONS_RELPATH",
    "OrgAgentExclusion",
    "OrgAgentExclusionAdoption",
    "OrgAgentExclusionStorageError",
    "OrgAgentExclusionStore",
    "derive_agent_org_labels",
    "exclusions_path",
    "export_agent_exclusions_bytes",
    "parse_transferred_exclusion_spaces",
    "read_carried_exclusion_spaces",
]


def _lock_for(path: Path) -> _PathLockEntry:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _LOCKS_GUARD:
        entry = _PATH_LOCKS.get(key)
        if entry is None:
            entry = _PathLockEntry(rlock=threading.RLock())
            _PATH_LOCKS[key] = entry
        return entry


def exclusions_path(hyprial_home: Path | str) -> Path:
    """Return ``<HYPRIAL_HOME>/state/org-agent-exclusions.json``."""

    return Path(hyprial_home) / "state" / ORG_AGENT_EXCLUSIONS_FILENAME


def _validate_non_system_ancestry(target: Path) -> None:
    lexical = Path(os.path.abspath(os.fspath(target)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise OrgAgentExclusionStorageError(
                f"cannot inspect exclusions ancestry {current}: {error}"
            ) from error
        if path_is_reparse(info):
            raise OrgAgentExclusionStorageError(
                f"refusing reparse point in exclusions ancestry: {current}"
            )
        if stat.S_ISLNK(info.st_mode):
            allowed = (
                current.anchor == "/"
                and current.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
                and current.is_dir()
            )
            if not allowed:
                raise OrgAgentExclusionStorageError(
                    f"refusing symlink in exclusions ancestry: {current}"
                )
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise OrgAgentExclusionStorageError(
                f"refusing non-directory in exclusions ancestry: {current}"
            )


def _require_private_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    kind: Literal["dir", "file"],
    mode: int,
    label: str,
) -> None:
    if stat.S_ISLNK(metadata.st_mode) or path_is_reparse(metadata):
        raise OrgAgentExclusionStorageError(
            f"refusing symlinked {label}: {path}"
        )
    if kind == "dir":
        if not stat.S_ISDIR(metadata.st_mode):
            raise OrgAgentExclusionStorageError(
                f"refusing non-directory {label}: {path}"
            )
    elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OrgAgentExclusionStorageError(
            f"refusing unsafe {label}: {path}"
        )
    try:
        violation = private_metadata_violation(path, metadata, mode=mode)
    except (PrivateAccessUnsupported, OSError) as error:
        raise OrgAgentExclusionStorageError(
            f"unsafe {label} {path}: {error}"
        ) from error
    if violation is not None:
        raise OrgAgentExclusionStorageError(
            f"refusing non-private {label} {path}: {violation}"
        )


def _validate_private_path(
    path: Path,
    *,
    kind: Literal["dir", "file"],
    mode: int,
    allow_missing: bool,
    label: str,
) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise OrgAgentExclusionStorageError(
            f"missing {label}: {path}"
        ) from None
    except OSError as error:
        raise OrgAgentExclusionStorageError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    _require_private_metadata(path, info, kind=kind, mode=mode, label=label)
    return info


def _validate_safe_directory(
    path: Path,
    *,
    allow_missing: bool,
    label: str,
) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise OrgAgentExclusionStorageError(
            f"missing {label}: {path}"
        ) from None
    except OSError as error:
        raise OrgAgentExclusionStorageError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or path_is_reparse(info):
        raise OrgAgentExclusionStorageError(
            f"refusing symlinked {label}: {path}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise OrgAgentExclusionStorageError(
            f"refusing non-directory {label}: {path}"
        )
    return info


def _ensure_private_directory(path: Path, *, label: str) -> None:
    existing = _validate_private_path(
        path, kind="dir", mode=0o700, allow_missing=True, label=label
    )
    if existing is not None:
        return
    try:
        secure_private_directory(path, create=True, mode=0o700)
    except FileExistsError:
        pass
    except (PrivateAccessUnsupported, OSError) as error:
        if isinstance(error, OrgAgentExclusionStorageError):
            raise
        raise OrgAgentExclusionStorageError(
            f"unsafe {label} {path}: {error}"
        ) from error
    _validate_private_path(
        path, kind="dir", mode=0o700, allow_missing=False, label=label
    )


def _read_verified_private_file(path: Path, *, label: str) -> bytes | None:
    before = _validate_private_path(
        path, kind="file", mode=0o600, allow_missing=True, label=label
    )
    if before is None:
        return None
    try:
        with open_private_file(path, mode=0o600) as (descriptor, opened):
            _require_private_metadata(
                path, opened, kind="file", mode=0o600, label=label
            )
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OrgAgentExclusionStorageError(
                    f"refusing replaced {label}: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
    except FileNotFoundError:
        return None
    except (PrivateAccessUnsupported, OSError) as error:
        if isinstance(error, OrgAgentExclusionStorageError):
            raise
        raise OrgAgentExclusionStorageError(
            f"unsafe {label} {path}: {error}"
        ) from error


def _open_and_lock_private_file(lock_path: Path, *, timeout: float) -> int:
    deadline = time.monotonic() + max(0.0, timeout)
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    while True:
        before = _validate_private_path(
            lock_path,
            kind="file",
            mode=0o600,
            allow_missing=True,
            label="exclusions lock file",
        )
        descriptor = -1
        if before is None:
            try:
                descriptor = os.open(
                    lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise OrgAgentExclusionStorageError(
                        f"cannot lock org exclusions store {lock_path}: contention timeout"
                    ) from None
                time.sleep(0.005)
                continue
            except OSError as error:
                raise OrgAgentExclusionStorageError(
                    f"cannot lock org exclusions store {lock_path}: {error}"
                ) from error
            try:
                harden_private_path(lock_path, mode=0o600)
                opened = os.fstat(descriptor)
                _require_private_metadata(
                    lock_path,
                    opened,
                    kind="file",
                    mode=0o600,
                    label="exclusions lock file",
                )
                after = _validate_private_path(
                    lock_path,
                    kind="file",
                    mode=0o600,
                    allow_missing=False,
                    label="exclusions lock file",
                )
                if after is None or (opened.st_dev, opened.st_ino) != (
                    after.st_dev,
                    after.st_ino,
                ):
                    raise OrgAgentExclusionStorageError(
                        f"refusing replaced exclusions lock file: {lock_path}"
                    )
            except BaseException as error:
                os.close(descriptor)
                if isinstance(error, (PrivateAccessUnsupported, OSError)):
                    if isinstance(error, OrgAgentExclusionStorageError):
                        raise
                    raise OrgAgentExclusionStorageError(
                        f"unsafe exclusions lock file {lock_path}: {error}"
                    ) from error
                raise
        else:
            try:
                descriptor = os.open(lock_path, flags)
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    raise OrgAgentExclusionStorageError(
                        f"cannot lock org exclusions store {lock_path}: disappeared"
                    ) from None
                time.sleep(0.005)
                continue
            except OSError as error:
                raise OrgAgentExclusionStorageError(
                    f"cannot lock org exclusions store {lock_path}: {error}"
                ) from error
            try:
                opened = os.fstat(descriptor)
                _require_private_metadata(
                    lock_path,
                    opened,
                    kind="file",
                    mode=0o600,
                    label="exclusions lock file",
                )
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OrgAgentExclusionStorageError(
                        f"refusing replaced exclusions lock file: {lock_path}"
                    )
            except BaseException:
                os.close(descriptor)
                raise

        try:
            while True:
                try:
                    lock_exclusive(descriptor, blocking=False)
                    break
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OrgAgentExclusionStorageError(
                            f"cannot lock org exclusions store {lock_path}: timeout"
                        ) from error
                    time.sleep(min(0.01, max(0.001, remaining)))
                except OSError as error:
                    raise OrgAgentExclusionStorageError(
                        f"cannot lock org exclusions store {lock_path}: {error}"
                    ) from error
        except BaseException:
            os.close(descriptor)
            raise

        try:
            current = _validate_private_path(
                lock_path,
                kind="file",
                mode=0o600,
                allow_missing=False,
                label="exclusions lock file",
            )
            locked_info = os.fstat(descriptor)
            _require_private_metadata(
                lock_path,
                locked_info,
                kind="file",
                mode=0o600,
                label="exclusions lock file",
            )
            if current is None or (locked_info.st_dev, locked_info.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise OrgAgentExclusionStorageError(
                    f"refusing replaced exclusions lock file: {lock_path}"
                )
            return descriptor
        except BaseException:
            _release_private_lock(descriptor)
            raise


def _release_private_lock(descriptor: int) -> None:
    try:
        unlock(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _validate_agent_id(value: object, label: str = "agentId") -> str:
    if not isinstance(value, str) or not is_agent_id_segment(value):
        raise ValueError(f"{label} must be a valid 'a.<id>' agent id")
    return value


def _validate_space_id(value: object, label: str = "directorySpace") -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class OrgAgentExclusion:
    """One per-(agent, org) exclusion entry keyed by agent id and space id."""

    agent_id: str
    directory_space: str

    def __post_init__(self) -> None:
        _validate_agent_id(self.agent_id, "agent_id")
        _validate_space_id(self.directory_space, "directory_space")

    def to_json(self) -> dict[str, str]:
        return {
            "agentId": self.agent_id,
            "directorySpace": self.directory_space,
        }

    @classmethod
    def from_json(
        cls, value: object, label: str = "org agent exclusion"
    ) -> "OrgAgentExclusion":
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object")
        expected = {"agentId", "directorySpace"}
        keys = set(value)
        if keys != expected:
            missing = expected - keys
            extra = keys - expected
            if missing:
                raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing))}")
            raise ValueError(f"{label} has unexpected keys: {', '.join(sorted(extra))}")
        return cls(
            agent_id=_validate_agent_id(value["agentId"], f"{label}.agentId"),
            directory_space=_validate_space_id(
                value["directorySpace"], f"{label}.directorySpace"
            ),
        )


def _decode_exclusions(payload: object) -> list[OrgAgentExclusion]:
    if not isinstance(payload, list):
        raise ValueError("org agent exclusions must be a JSON array")
    seen: dict[tuple[str, str], OrgAgentExclusion] = {}
    for index, item in enumerate(payload):
        row = OrgAgentExclusion.from_json(item, f"exclusions[{index}]")
        seen[(row.agent_id, row.directory_space)] = row
    return [seen[key] for key in sorted(seen)]


def _serialize_rows(rows: Iterable[OrgAgentExclusion]) -> list[dict[str, str]]:
    unique = {
        (row.agent_id, row.directory_space): row
        for row in rows
    }
    return [unique[key].to_json() for key in sorted(unique)]


class OrgAgentExclusionStore:
    """Local atomic persistence for ``<HYPRIAL_HOME>/state/org-agent-exclusions.json``."""

    def __init__(self, hyprial_home: Path | str) -> None:
        self._home = Path(hyprial_home)
        self._path = exclusions_path(self._home)
        self._lock_path = self._path.parent / f".{ORG_AGENT_EXCLUSIONS_FILENAME}.lock"

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> tuple[OrgAgentExclusion, ...]:
        entry = _lock_for(self._path)
        with entry.rlock:
            return tuple(self._load_locked())

    def excluded_agent_ids(self, directory_space: str) -> frozenset[str]:
        space_id = _validate_space_id(directory_space)
        return frozenset(
            row.agent_id
            for row in self.list()
            if row.directory_space == space_id
        )

    def excluded_spaces(self, agent_id: str) -> frozenset[str]:
        valid_agent = _validate_agent_id(agent_id)
        return frozenset(
            row.directory_space
            for row in self.list()
            if row.agent_id == valid_agent
        )

    def is_excluded(self, agent_id: str, directory_space: str) -> bool:
        valid_agent = _validate_agent_id(agent_id)
        space_id = _validate_space_id(directory_space)
        return any(
            row.agent_id == valid_agent and row.directory_space == space_id
            for row in self.list()
        )

    def for_agent(self, agent_id: str) -> tuple[OrgAgentExclusion, ...]:
        valid_agent = _validate_agent_id(agent_id)
        return tuple(
            row for row in self.list() if row.agent_id == valid_agent
        )

    def exclude(self, agent_id: str, directory_space: str) -> bool:
        entry = OrgAgentExclusion(
            agent_id=_validate_agent_id(agent_id),
            directory_space=_validate_space_id(directory_space),
        )
        with self._locked_mutation():
            rows = self._load_locked()
            if entry in rows:
                return False
            rows.append(entry)
            self._save_locked(rows)
            return True

    def include(self, agent_id: str, directory_space: str) -> bool:
        valid_agent = _validate_agent_id(agent_id)
        space_id = _validate_space_id(directory_space)
        with self._locked_mutation():
            rows = self._load_locked()
            filtered = [
                row
                for row in rows
                if not (
                    row.agent_id == valid_agent and row.directory_space == space_id
                )
            ]
            if len(filtered) == len(rows):
                return False
            self._save_locked(filtered)
            return True

    def adopt_for_agent(
        self,
        agent_id: str,
        directory_spaces: Iterable[str],
        *,
        on_committed: Callable[[OrgAgentExclusionAdoption], None] | None = None,
    ) -> OrgAgentExclusionAdoption:
        valid_agent = _validate_agent_id(agent_id)
        unique_spaces = tuple(
            sorted({_validate_space_id(space_id) for space_id in directory_spaces})
        )
        if not unique_spaces:
            self._validate_store_paths(create_state_dir=False)
            return OrgAgentExclusionAdoption(
                store=self, agent_id=valid_agent, added_spaces=()
            )
        with self._locked_mutation():
            rows = self._load_locked()
            existing = set(rows)
            added: list[str] = []
            for space_id in unique_spaces:
                entry = OrgAgentExclusion(
                    agent_id=valid_agent,
                    directory_space=space_id,
                )
                if entry not in existing:
                    rows.append(entry)
                    existing.add(entry)
                    added.append(space_id)
            adoption = OrgAgentExclusionAdoption(
                store=self,
                agent_id=valid_agent,
                added_spaces=tuple(added),
            )
            if not added:
                return adoption
            published = False

            def _mark_published() -> None:
                nonlocal published
                published = True
                if on_committed is not None:
                    on_committed(adoption)

            try:
                self._save_locked(rows, on_published=_mark_published)
            except OrgAgentExclusionStorageError as error:
                if published and error.committed_adoption is None:
                    raise OrgAgentExclusionStorageError(
                        str(error), committed_adoption=adoption
                    ) from error
                raise
            return adoption

    def remove_for_agent(
        self, agent_id: str, directory_spaces: Iterable[str]
    ) -> bool:
        valid_agent = _validate_agent_id(agent_id)
        targets = {_validate_space_id(space_id) for space_id in directory_spaces}
        if not targets:
            self._validate_store_paths(create_state_dir=False)
            return False
        with self._locked_mutation():
            rows = self._load_locked()
            filtered = [
                row
                for row in rows
                if not (row.agent_id == valid_agent and row.directory_space in targets)
            ]
            if len(filtered) == len(rows):
                return False
            self._save_locked(filtered)
            return True

    def remove_all_for_agent(self, agent_id: str) -> bool:
        valid_agent = _validate_agent_id(agent_id)
        with self._locked_mutation():
            rows = self._load_locked()
            filtered = [row for row in rows if row.agent_id != valid_agent]
            if len(filtered) == len(rows):
                return False
            self._save_locked(filtered)
            return True

    @contextmanager
    def _locked_mutation(self) -> Iterator[None]:
        entry = _lock_for(self._path)
        with entry.rlock:
            self._validate_store_paths(create_state_dir=True)
            if entry.depth == 0:
                entry.lock_fd = _open_and_lock_private_file(
                    self._lock_path, timeout=_LOCK_TIMEOUT_SECONDS
                )
            entry.depth += 1
            try:
                self._validate_store_paths(create_state_dir=True)
                yield
            finally:
                entry.depth -= 1
                if entry.depth == 0:
                    held = entry.lock_fd
                    entry.lock_fd = None
                    if held is not None:
                        _release_private_lock(held)

    def _validate_store_paths(self, *, create_state_dir: bool) -> bool:
        _validate_non_system_ancestry(self._home)
        state_dir = self._path.parent
        if create_state_dir:
            _ensure_private_directory(
                self._home, label="exclusions home directory"
            )
            _ensure_private_directory(
                state_dir, label="exclusions state directory"
            )
        else:
            home_info = _validate_private_path(
                self._home,
                kind="dir",
                mode=0o700,
                allow_missing=True,
                label="exclusions home directory",
            )
            if home_info is None:
                return False
            state_info = _validate_private_path(
                state_dir,
                kind="dir",
                mode=0o700,
                allow_missing=True,
                label="exclusions state directory",
            )
            if state_info is None:
                return False
        _validate_private_path(
            self._lock_path,
            kind="file",
            mode=0o600,
            allow_missing=True,
            label="exclusions lock file",
        )
        file_info = _validate_private_path(
            self._path,
            kind="file",
            mode=0o600,
            allow_missing=True,
            label="exclusions file",
        )
        return file_info is not None

    def _load_locked(self) -> list[OrgAgentExclusion]:
        if not self._validate_store_paths(create_state_dir=False):
            return []
        raw_bytes = _read_verified_private_file(
            self._path, label="exclusions file"
        )
        if raw_bytes is None:
            return []
        raw = json.loads(raw_bytes.decode("utf-8"))
        return _decode_exclusions(raw)

    def _save_locked(
        self,
        rows: list[OrgAgentExclusion],
        *,
        on_published: Callable[[], None] | None = None,
    ) -> None:
        self._validate_store_paths(create_state_dir=True)
        payload = (
            json.dumps(_serialize_rows(rows), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor = -1
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{ORG_AGENT_EXCLUSIONS_FILENAME}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temp_path = Path(temp_name)
            harden_private_path(temp_path, mode=0o600)
            opened = os.fstat(descriptor)
            _require_private_metadata(
                temp_path,
                opened,
                kind="file",
                mode=0o600,
                label="exclusions temporary file",
            )
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temp_path, self._path)
            temp_path = None
            if on_published is not None:
                on_published()
            fsync_directory(self._path.parent)
        except (PrivateAccessUnsupported, OSError) as error:
            if isinstance(error, OrgAgentExclusionStorageError):
                raise
            raise OrgAgentExclusionStorageError(
                f"cannot write exclusions file {self._path}: {error}"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


def export_agent_exclusions_bytes(
    hyprial_home: Path | str,
    agent_id: str,
    *,
    agent_home: Path | str | None = None,
) -> bytes | None:
    """Serialize one agent's exclusions for transfer bundle export, or None."""

    valid_agent = _validate_agent_id(agent_id)
    spaces = set(OrgAgentExclusionStore(hyprial_home).excluded_spaces(valid_agent))
    if agent_home is not None:
        carried_spaces = read_carried_exclusion_spaces(agent_home)
        if carried_spaces:
            spaces.update(carried_spaces)
    if not spaces:
        return None
    rows = [
        OrgAgentExclusion(agent_id=valid_agent, directory_space=space_id)
        for space_id in sorted(spaces)
    ]
    payload = json.dumps(_serialize_rows(rows), separators=(",", ":")).encode("utf-8")
    return payload


def parse_transferred_exclusion_spaces(payload: bytes) -> tuple[str, ...]:
    """Decode transferred exclusion entries into deduplicated directory space ids."""

    rows = _decode_exclusions(json.loads(payload.decode("utf-8")))
    return tuple(sorted({row.directory_space for row in rows}))


def read_carried_exclusion_spaces(
    agent_home: Path | str, *, require_private_dirs: bool = True
) -> tuple[str, ...] | None:
    """Validate and decode ``state/org-agent-exclusions.json`` without following symlinks."""

    home = Path(agent_home)
    _validate_non_system_ancestry(home)
    dir_validator = (
        (
            lambda path, label: _validate_private_path(
                path,
                kind="dir",
                mode=0o700,
                allow_missing=True,
                label=label,
            )
        )
        if require_private_dirs
        else (
            lambda path, label: _validate_safe_directory(
                path,
                allow_missing=True,
                label=label,
            )
        )
    )
    if dir_validator(home, "carried exclusions home directory") is None:
        return None
    state_dir = home / "state"
    if dir_validator(state_dir, "carried exclusions state directory") is None:
        return None
    carried = state_dir / ORG_AGENT_EXCLUSIONS_FILENAME
    payload = _read_verified_private_file(
        carried, label="carried exclusions file"
    )
    if payload is None:
        return None
    return parse_transferred_exclusion_spaces(payload)


def _bare_owner(user: str) -> str:
    return directory_owner_principal(user).removeprefix("user:")


def _owner_active_org_spaces(
    app: Any, *, repair_current: bool = False
) -> dict[str, str]:
    owner_value = getattr(app, "owner", None)
    if not isinstance(owner_value, str) or not owner_value:
        return {}
    try:
        facade = app._require_orgfs_runtime().facade
        active = for_app(app, repair_current=repair_current).active
    except (DaemonRequestError, OrgFsError, RuntimeError):
        return {}
    owner = _bare_owner(owner_value)
    spaces_by_name = {space.name: space for space in facade.spaces()}
    confirmed: dict[str, str] = {}
    for org, row in active.items():
        space = spaces_by_name.get(org)
        if space is None:
            continue
        space_id = str(row.get("spaceId") or space.space_id)
        if not space_id:
            continue
        try:
            members = {
                _bare_owner(space.owner),
                *(_bare_owner(member.user) for member in facade.members(space.space_id)),
            }
        except (OrgFsError, RuntimeError, ValueError):
            continue
        if owner in members:
            confirmed[org] = space_id
    return confirmed


def derive_agent_org_labels(
    app: Any, agent_id: str, *, repair_current: bool = False
) -> tuple[str, ...]:
    """Derive active org labels from owner confirmation minus local exclusions."""

    valid_agent = _validate_agent_id(agent_id, "agent_id")
    active = _owner_active_org_spaces(app, repair_current=repair_current)
    if not active:
        return ()
    home = getattr(app, "hyprial_home", None)
    excluded = (
        OrgAgentExclusionStore(Path(home)).excluded_spaces(valid_agent)
        if home
        else frozenset()
    )
    return tuple(
        sorted(org for org, space_id in active.items() if space_id not in excluded)
    )
