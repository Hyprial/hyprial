# Decision record (J22 local credential/grant storage)
# - Model credential records and grants belong only on the local machine under
#   <HYPRIAL_HOME>/state/provider-keys/store/<ownerKey>/, never in an OrgFS
#   personal-org or owner-home space.
# - Writes require exactly one of create_only or expect_version and enforce
#   compare-and-set atomically across threads and processes using a per-root
#   ProcessFileLock, unique temp files, and a single atomic commit record that
#   binds version and content digest before materializing target and marker.
# - Fail closed on missing, non-private, or symlinked hyprial_home and on any
#   symlink/reparse ancestor, backslash traversal, containment escape, or torn pair.
"""Local state-directory OwnerStore and OwnerSpaceResolver."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from hyprial.identity import ProviderKeys
from hyprial.kernel import (
    PrivateAccessUnsupported,
    ProcessFileLock,
    current_uid,
    fsync_directory,
    harden_private_path,
    ipc_errors,
    open_private_file,
    path_is_reparse,
    posix_metadata_enforced,
    private_metadata_violation,
    read_private_file,
    write_private_file_atomic,
)
from .port import InvalidSegment, StaleWrite, check_store_path

_SAFE_OWNER = re.compile(r"[^A-Za-z0-9._-]+")
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_TIMEOUT_S = 30.0
_HEX64 = re.compile(r"[0-9a-f]{64}")
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


def _provider_unavailable(_reason: str = "") -> ProviderKeys.Error:
    return ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def _thread_lock_for(root: Path) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(root)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[root] = lock
        return lock


def _is_allowed_system_symlink(step: Path, target: Path) -> bool:
    return (
        step != target
        and step.anchor == "/"
        and step.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
    )


def require_private_home(
    home: Path | str | None,
    error_factory: Callable[[str], BaseException],
) -> Path:
    """Validate an existing, private, non-symlink ``hyprial_home`` and return its canonical path."""
    if home is None or (isinstance(home, str) and not home.strip()):
        raise error_factory("home-missing")
    raw_str = os.fspath(home)
    if not raw_str or "\x00" in str(raw_str):
        raise error_factory("home-missing")
    lexical = Path(os.path.abspath(raw_str))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except FileNotFoundError:
            raise error_factory("home-missing") from None
        except OSError:
            raise error_factory("home-unavailable") from None
        if path_is_reparse(observed):
            raise error_factory("unsafe-symlink")
        if stat.S_ISLNK(observed.st_mode):
            if not _is_allowed_system_symlink(current, lexical) or not current.is_dir():
                raise error_factory("unsafe-symlink")
    try:
        observed = lexical.lstat()
    except OSError:
        raise error_factory("home-missing") from None
    if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
        raise error_factory("unsafe-symlink")
    if not stat.S_ISDIR(observed.st_mode):
        raise error_factory("home-not-directory")
    canonical = lexical.resolve()
    try:
        canonical_stat = canonical.lstat()
    except OSError:
        raise error_factory("home-unavailable") from None
    violation = private_metadata_violation(canonical, canonical_stat, mode=_DIR_MODE)
    if violation is not None:
        raise error_factory(violation)
    return canonical


def ensure_private_directory_entry(
    path: Path,
    observed: os.stat_result,
    error_factory: Callable[[str], BaseException],
) -> None:
    """Validate an existing directory entry and enforce ``0o700`` when owned."""
    if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed) or not stat.S_ISDIR(observed.st_mode):
        raise error_factory("unsafe-path")
    violation = private_metadata_violation(path, observed, mode=_DIR_MODE)
    if violation is None:
        return
    if violation in {"wrong-owner", "unsafe-reparse", "unsupported-private-acl"} or (
        posix_metadata_enforced() and observed.st_uid != current_uid()
    ):
        raise error_factory(violation)
    try:
        harden_private_path(path, mode=_DIR_MODE)
    except (OSError, PrivateAccessUnsupported):
        raise error_factory("unsafe-permissions") from None


def ensure_private_dir_chain(
    canonical_anchor: Path,
    target: Path,
    error_factory: Callable[[str], BaseException],
    *,
    create: bool,
) -> Path | None:
    """Walk from ``canonical_anchor`` to ``target`` rejecting symlinks and enforcing 0700."""
    try:
        rel = target.relative_to(canonical_anchor)
    except ValueError:
        raise error_factory("outside-root") from None
    if any(part in ("", ".", "..") for part in rel.parts):
        raise error_factory("unsafe-path")
    current = canonical_anchor
    steps = (canonical_anchor, *(canonical_anchor.joinpath(*rel.parts[: i + 1]) for i in range(len(rel.parts))))
    for step in steps:
        try:
            observed = step.lstat()
        except FileNotFoundError:
            if not create:
                return None
            try:
                step.mkdir(mode=_DIR_MODE)
                harden_private_path(step, mode=_DIR_MODE)
            except (OSError, PrivateAccessUnsupported):
                raise error_factory("dir-create-failed") from None
            current = step
            continue
        except OSError:
            raise error_factory("unsafe-path") from None
        ensure_private_directory_entry(step, observed, error_factory)
        current = step
    resolved = current.resolve()
    if resolved != canonical_anchor and not resolved.is_relative_to(canonical_anchor):
        raise error_factory("outside-root")
    return resolved


def validate_no_symlink_descendant(
    canonical_root: Path,
    parts: tuple[str, ...],
    error_factory: Callable[[str], BaseException],
    *,
    expect_leaf_dir: bool = False,
) -> Path | None:
    """Validate every existing path segment from ``canonical_root`` through ``parts``."""
    current = canonical_root
    for idx, part in enumerate(parts):
        current = current / part
        is_last = idx == len(parts) - 1
        try:
            observed = current.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise error_factory("unsafe-path") from None
        if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
            raise error_factory("unsafe-path")
        if not is_last or expect_leaf_dir or stat.S_ISDIR(observed.st_mode):
            ensure_private_directory_entry(current, observed, error_factory)
    resolved = current.resolve()
    if resolved != canonical_root and not resolved.is_relative_to(canonical_root):
        raise error_factory("outside-root")
    return resolved


def _owner_dir_name(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip() or "\0" in owner:
        raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
    clean = _SAFE_OWNER.sub("_", owner.strip()).strip("._-") or "owner"
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    return f"{clean[:32]}-{digest}"


def _format_ver(version: str, sha256_hex: str) -> bytes:
    return f"{version}:{sha256_hex}".encode("utf-8")


def _parse_ver(raw: bytes) -> tuple[str, str]:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise _provider_unavailable("corrupt-version") from None
    if ":" not in text:
        raise _provider_unavailable("corrupt-version")
    version, digest = text.split(":", 1)
    if not version or _HEX64.fullmatch(digest) is None:
        raise _provider_unavailable("corrupt-version")
    return version, digest


class LocalOwnerStore:
    """OwnerStore backed by a private directory under ``<HYPRIAL_HOME>/state/``."""

    def __init__(self, root: Path, *, home: Path | None = None) -> None:
        self._root = Path(root)
        self._home = Path(home) if home is not None else None

    def __repr__(self) -> str:
        return f"LocalOwnerStore(root={str(self._root)!r})"

    def _canonical_root(self, *, create: bool) -> Path | None:
        if self._home is not None:
            canonical_home = require_private_home(self._home, _provider_unavailable)
            rel_root = self._root
            if not rel_root.is_absolute():
                raise _provider_unavailable("invalid-root")
            try:
                rel = rel_root.relative_to(self._home)
            except ValueError:
                rel = rel_root.resolve().relative_to(canonical_home)
            return ensure_private_dir_chain(
                canonical_home, canonical_home / rel, _provider_unavailable, create=create
            )
        lexical = Path(os.path.abspath(os.fspath(self._root)))
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            current /= part
            try:
                observed = current.lstat()
            except FileNotFoundError:
                if not create:
                    return None
                raise _provider_unavailable("missing-root") from None
            except OSError:
                raise _provider_unavailable("unsafe-path") from None
            if path_is_reparse(observed):
                raise _provider_unavailable("unsafe-symlink")
            if stat.S_ISLNK(observed.st_mode):
                if not _is_allowed_system_symlink(current, lexical) or not current.is_dir():
                    raise _provider_unavailable("unsafe-symlink")
        resolved = ensure_private_dir_chain(
            lexical.resolve() if lexical.exists() else lexical,
            lexical.resolve() if lexical.exists() else lexical,
            _provider_unavailable,
            create=create,
        )
        return resolved

    @staticmethod
    def _prepare_lock_file(lock_path: Path) -> None:
        try:
            lock_stat = lock_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise _provider_unavailable("lock-unavailable") from None
        if (
            stat.S_ISLNK(lock_stat.st_mode)
            or path_is_reparse(lock_stat)
            or not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
        ):
            raise _provider_unavailable("unsafe-lock")
        violation = private_metadata_violation(lock_path, lock_stat, mode=_FILE_MODE)
        if violation is not None:
            if violation in {"wrong-owner", "unsafe-reparse", "unsupported-private-acl"} or (
                posix_metadata_enforced() and lock_stat.st_uid != current_uid()
            ):
                raise _provider_unavailable("unsafe-lock")
            try:
                harden_private_path(lock_path, mode=_FILE_MODE)
            except (OSError, PrivateAccessUnsupported):
                raise _provider_unavailable("unsafe-lock") from None
        try:
            with open_private_file(lock_path, mode=_FILE_MODE):
                pass
        except (OSError, PrivateAccessUnsupported):
            raise _provider_unavailable("unsafe-lock") from None

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[Path | None]:
        canonical_root = self._canonical_root(create=create)
        if canonical_root is None:
            yield None
            return
        versions_dir = ensure_private_dir_chain(
            canonical_root, canonical_root / ".versions", _provider_unavailable, create=create
        )
        if create and versions_dir is None:
            raise _provider_unavailable("versions-missing")
        lock_path = canonical_root / ".cas.lock"
        tlock = _thread_lock_for(canonical_root)
        if not tlock.acquire(timeout=_LOCK_TIMEOUT_S):
            raise _provider_unavailable("lock-busy")
        plock = ProcessFileLock(lock_path, mode=_FILE_MODE)
        try:
            self._prepare_lock_file(lock_path)
            try:
                plock.acquire(timeout=_LOCK_TIMEOUT_S, poll_interval=0.005)
            except OSError:
                raise _provider_unavailable("lock-busy") from None
            try:
                with open_private_file(lock_path, mode=_FILE_MODE):
                    pass
            except (OSError, PrivateAccessUnsupported):
                raise _provider_unavailable("unsafe-lock") from None
            yield canonical_root
        finally:
            plock.release()
            tlock.release()

    @staticmethod
    def _record_paths(canonical_root: Path, path: str) -> tuple[Path, Path, Path, Path]:
        versions = canonical_root / ".versions"
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        target = canonical_root.joinpath(*path.split("/"))
        return target, versions / f"{digest}.ver", versions / f"{digest}.commit", versions

    @staticmethod
    def _recover_commit(
        canonical_root: Path,
        parts: tuple[str, ...],
        target: Path,
        ver_file: Path,
        commit_file: Path,
    ) -> None:
        try:
            commit_stat = commit_file.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise _provider_unavailable("commit-unavailable") from None
        if stat.S_ISLNK(commit_stat.st_mode) or path_is_reparse(commit_stat) or not stat.S_ISREG(commit_stat.st_mode):
            raise _provider_unavailable("unsafe-commit")
        try:
            raw, _ = read_private_file(commit_file, mode=_FILE_MODE)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, PrivateAccessUnsupported, ValueError):
            raise _provider_unavailable("corrupt-commit") from None
        if not isinstance(payload, dict):
            raise _provider_unavailable("corrupt-commit")
        version = payload.get("version")
        digest = payload.get("sha256")
        text = payload.get("text")
        if not isinstance(version, str) or not version or not isinstance(digest, str) or not isinstance(text, str):
            raise _provider_unavailable("corrupt-commit")
        encoded = text.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise _provider_unavailable("corrupt-commit")
        ensure_private_dir_chain(canonical_root, target.parent, _provider_unavailable, create=True)
        validate_no_symlink_descendant(canonical_root, parts, _provider_unavailable)
        try:
            write_private_file_atomic(ver_file, _format_ver(version, digest), mode=_FILE_MODE)
            write_private_file_atomic(target, encoded, mode=_FILE_MODE)
            commit_file.unlink(missing_ok=True)
            fsync_directory(commit_file.parent)
        except (OSError, PrivateAccessUnsupported):
            raise _provider_unavailable("commit-recovery-failed") from None

    @classmethod
    def _read_locked(
        cls,
        canonical_root: Path,
        path: str,
    ) -> tuple[str, str] | None:
        parts = tuple(path.split("/"))
        validate_no_symlink_descendant(canonical_root, parts, _provider_unavailable)
        target, ver_file, commit_file, versions = cls._record_paths(canonical_root, path)
        if versions.exists():
            cls._recover_commit(canonical_root, parts, target, ver_file, commit_file)
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            try:
                ver_file.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                raise _provider_unavailable("torn-record") from None
            raise _provider_unavailable("torn-record") from None
        except OSError:
            raise _provider_unavailable("read-failed") from None
        if stat.S_ISLNK(target_stat.st_mode) or path_is_reparse(target_stat) or not stat.S_ISREG(target_stat.st_mode):
            raise _provider_unavailable("unsafe-file")
        try:
            ver_file.lstat()
        except FileNotFoundError:
            raise _provider_unavailable("torn-record") from None
        except OSError:
            raise _provider_unavailable("torn-record") from None
        try:
            data, _ = read_private_file(target, mode=_FILE_MODE)
            ver_bytes, _ = read_private_file(ver_file, mode=_FILE_MODE)
        except (OSError, PrivateAccessUnsupported):
            raise _provider_unavailable("read-failed") from None
        version, expected_sha = _parse_ver(ver_bytes)
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise _provider_unavailable("torn-record")
        try:
            return data.decode("utf-8"), version
        except UnicodeDecodeError:
            raise _provider_unavailable("corrupt-text") from None

    def read(self, path: str) -> tuple[str, str] | None:
        """Return ``(text, version)`` or ``None`` when absent."""
        check_store_path(path)
        with self._locked(create=False) as canonical_root:
            if canonical_root is None:
                return None
            return self._read_locked(canonical_root, path)

    def version_of(self, path: str) -> str | None:
        """Return the current CAS version token or ``None`` when absent."""
        found = self.read(path)
        return None if found is None else found[1]

    def write(self, path: str, text: str, *, expect_version: str | None, create_only: bool) -> str:
        """Write atomically with ``create_only`` or ``expect_version`` CAS."""
        check_store_path(path)
        if bool(create_only) == (expect_version is not None):
            raise InvalidSegment()
        parts = tuple(path.split("/"))
        with self._locked(create=True) as canonical_root:
            assert canonical_root is not None
            current = self._read_locked(canonical_root, path)
            if create_only:
                if current is not None:
                    raise StaleWrite()
            else:
                if current is None or current[1] != expect_version:
                    raise StaleWrite()
            target, ver_file, commit_file, versions = self._record_paths(canonical_root, path)
            ensure_private_dir_chain(canonical_root, target.parent, _provider_unavailable, create=True)
            validate_no_symlink_descendant(canonical_root, parts, _provider_unavailable)
            version = uuid.uuid4().hex
            encoded = text.encode("utf-8")
            content_sha = hashlib.sha256(encoded).hexdigest()
            commit_payload = json.dumps(
                {"version": version, "sha256": content_sha, "text": text},
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                write_private_file_atomic(commit_file, commit_payload, mode=_FILE_MODE)
                write_private_file_atomic(ver_file, _format_ver(version, content_sha), mode=_FILE_MODE)
                write_private_file_atomic(target, encoded, mode=_FILE_MODE)
                commit_file.unlink(missing_ok=True)
                fsync_directory(versions)
            except (OSError, PrivateAccessUnsupported):
                raise _provider_unavailable("write-failed") from None
            return version

    def remove(self, path: str) -> None:
        """Remove the record and its version marker when present."""
        check_store_path(path)
        parts = tuple(path.split("/"))
        with self._locked(create=False) as canonical_root:
            if canonical_root is None:
                return
            validate_no_symlink_descendant(canonical_root, parts, _provider_unavailable)
            target, ver_file, commit_file, _ = self._record_paths(canonical_root, path)
            try:
                commit_file.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                ver_file.unlink(missing_ok=True)
            except OSError:
                raise _provider_unavailable("remove-failed") from None

    def listdir(self, path: str) -> tuple[str, ...]:
        """Return sorted child names in ``path``; empty tuple when absent."""
        check_store_path(path)
        parts = tuple(path.split("/"))
        with self._locked(create=False) as canonical_root:
            if canonical_root is None:
                return ()
            resolved = validate_no_symlink_descendant(
                canonical_root, parts, _provider_unavailable, expect_leaf_dir=True
            )
            if resolved is None:
                return ()
            names: list[str] = []
            try:
                for entry in resolved.iterdir():
                    if entry.name.startswith("."):
                        continue
                    observed = entry.lstat()
                    if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
                        raise _provider_unavailable("unsafe-child")
                    if stat.S_ISDIR(observed.st_mode):
                        ensure_private_directory_entry(entry, observed, _provider_unavailable)
                    names.append(entry.name)
            except OSError:
                raise _provider_unavailable("listdir-failed") from None
            return tuple(sorted(names))


class LocalSpaceResolver:
    """Resolve an owner subject to its local ``<HYPRIAL_HOME>/state/`` store."""

    def __init__(self, home: Path) -> None:
        self._home = require_private_home(home, _provider_unavailable)
        self._base = self._home / "state" / "provider-keys" / "store"
        ensure_private_dir_chain(self._home, self._base, _provider_unavailable, create=False)

    def store_for(self, owner: str) -> LocalOwnerStore:
        """Return the local ``OwnerStore`` for ``owner``."""
        canonical_home = require_private_home(self._home, _provider_unavailable)
        base = canonical_home / "state" / "provider-keys" / "store"
        ensure_private_dir_chain(canonical_home, base, _provider_unavailable, create=False)
        owner_root = base / _owner_dir_name(owner)
        ensure_private_dir_chain(canonical_home, owner_root, _provider_unavailable, create=False)
        return LocalOwnerStore(owner_root, home=canonical_home)

    def __repr__(self) -> str:
        return f"LocalSpaceResolver(home={str(self._home)!r})"
