"""Safe archive extraction and lifecycle helpers for pinned hc copies."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tarfile
import uuid
import zipfile
from collections.abc import Iterable

from hyprial.kernel import create_relative_file_symlink
from hyprial.kernel import hc_bin_root
from hyprial.kernel import hc_version_root
from hyprial.kernel import host_hc_release_target
from hyprial.kernel import mark_executable
from hyprial.kernel import secure_private_directory
from hyprial.kernel import unregister_macos_app_bundle

from .download import download_hc_archive
from .lock import HcAcquisitionError
from .lock import HcLock
from .lock import load_bundled_hc_lock


def _target_name(target: str | None) -> str:
    resolved = target or host_hc_release_target()
    if resolved is None:
        raise HcAcquisitionError("unable to resolve the host hc target")
    return resolved


def _archive_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or PureWindowsPath(normalized).drive:
        raise HcAcquisitionError("hc archive contains an absolute member path")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise HcAcquisitionError("hc archive contains an unsafe member path")
    return path


def _link_target_path(member_path: PurePosixPath, target: str) -> PurePosixPath:
    normalized = target.replace("\\", "/")
    if not normalized or normalized.startswith("/") or PureWindowsPath(normalized).drive:
        raise HcAcquisitionError("hc archive contains an unsafe symlink")
    parts = list(member_path.parent.parts)
    for part in PurePosixPath(normalized).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise HcAcquisitionError("hc archive symlink escapes extraction root")
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts)


def _validate_tar_members(members: Iterable[tarfile.TarInfo]) -> tuple[tarfile.TarInfo, ...]:
    validated: list[tarfile.TarInfo] = []
    for member in members:
        path = _archive_member_path(member.name)
        if member.islnk() or member.isdev() or member.ischr() or member.isblk() or member.isfifo():
            raise HcAcquisitionError("hc archive contains an unsupported tar member")
        if member.issym():
            _link_target_path(path, member.linkname)
        validated.append(member)
    return tuple(validated)


def _validate_zip_member(info: zipfile.ZipInfo) -> PurePosixPath:
    path = _archive_member_path(info.filename)
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        _link_target_path(path, "placeholder")
    return path


def _selected_path(path: PurePosixPath, target: str) -> bool:
    if target.startswith("macos-"):
        return path == PurePosixPath("Herdr Core.app") or path.parts[:1] == ("Herdr Core.app",)
    if target.startswith("linux-"):
        return path == PurePosixPath("herdr-core")
    return path == PurePosixPath("herdr-core.exe") or path.parts[:1] == ("conpty",)


def _ensure_parent(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise HcAcquisitionError("hc archive writes through a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_tar_members(archive: tarfile.TarFile, members: Iterable[tarfile.TarInfo], root: Path, target: str) -> None:
    for member in members:
        path = _archive_member_path(member.name)
        if not _selected_path(path, target):
            continue
        destination = root.joinpath(*path.parts)
        if member.isdir():
            _ensure_parent(root, destination / "placeholder")
            destination.mkdir(exist_ok=True)
            continue
        _ensure_parent(root, destination)
        if destination.exists() or destination.is_symlink():
            raise HcAcquisitionError("hc archive contains duplicate members")
        if member.issym():
            create_relative_file_symlink(member.linkname, destination)
            continue
        source = archive.extractfile(member)
        if source is None:
            raise HcAcquisitionError("hc archive member cannot be read")
        with source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
        destination.chmod(member.mode & 0o7777)


def _write_zip_members(archive: zipfile.ZipFile, root: Path, target: str) -> None:
    for info in archive.infolist():
        path = _validate_zip_member(info)
        if not _selected_path(path, target):
            continue
        destination = root.joinpath(*path.parts)
        mode = (info.external_attr >> 16) & 0o170000
        if info.is_dir():
            _ensure_parent(root, destination / "placeholder")
            destination.mkdir(exist_ok=True)
            continue
        _ensure_parent(root, destination)
        if destination.exists() or destination.is_symlink():
            raise HcAcquisitionError("hc archive contains duplicate members")
        if mode == stat.S_IFLNK:
            link_target = archive.read(info).decode("utf-8")
            _link_target_path(path, link_target)
            create_relative_file_symlink(link_target, destination)
            continue
        with archive.open(info) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
        permission = (info.external_attr >> 16) & 0o7777
        if permission:
            destination.chmod(permission)


def _has_expected_executable(root: Path, target: str) -> bool:
    if target.startswith("macos-"):
        executable = root / "Herdr Core.app" / "Contents" / "MacOS" / "herdr-core"
    elif target.startswith("linux-"):
        executable = root / "herdr-core"
    else:
        executable = root / "herdr-core.exe"
    return executable.is_file() and not executable.is_symlink()


def extract_hc_archive(
    archive_path: Path,
    target: str,
    home: Path,
    version: str,
) -> Path:
    """Extract a verified hc archive into a private staging directory and commit it."""
    bin_root = secure_private_directory(hc_bin_root(home), create=True)
    version_root = hc_version_root(version, home)
    if version_root.exists() and _has_expected_executable(version_root, target):
        return version_root
    staging = bin_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        if target.startswith(("macos-", "linux-")):
            with tarfile.open(archive_path, "r:*") as archive:
                members = _validate_tar_members(archive.getmembers())
                _write_tar_members(archive, members, staging, target)
        elif target == "windows-x86_64":
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    _validate_zip_member(info)
                _write_zip_members(archive, staging, target)
        else:
            raise HcAcquisitionError(f"unsupported hc extraction target: {target}")
        if not _has_expected_executable(staging, target):
            raise HcAcquisitionError("hc archive has no expected executable")
        if target.startswith(("linux-", "windows-")):
            executable = staging / ("herdr-core" if target.startswith("linux-") else "herdr-core.exe")
            mark_executable(executable)
        try:
            os.replace(staging, version_root)
        except FileExistsError:
            if not _has_expected_executable(version_root, target):
                raise
        return version_root
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def installed_hc_executable(
    version: str,
    home: Path | None = None,
    target: str | None = None,
) -> Path:
    """Return the executable path for one installed pinned version."""
    resolved_target = _target_name(target)
    root = hc_version_root(version, home)
    if resolved_target.startswith("macos-"):
        return root / "Herdr Core.app" / "Contents" / "MacOS" / "herdr-core"
    if resolved_target.startswith("linux-"):
        return root / "herdr-core"
    return root / "herdr-core.exe"


def ensure_pinned_hc_installed(
    home: Path | None = None,
    target: str | None = None,
    lock: HcLock | None = None,
    *,
    opener: object | None = None,
    sleep: object | None = None,
) -> Path:
    """Ensure the current host's pinned hc executable is downloaded and installed."""
    resolved_lock = lock or load_bundled_hc_lock()
    target_name = _target_name(target)
    executable = installed_hc_executable(resolved_lock.version, home, target_name)
    if executable.is_file() and not executable.is_symlink():
        return executable
    bin_root = secure_private_directory(hc_bin_root(home), create=True)
    archive = bin_root / f".archive-{uuid.uuid4().hex}"
    try:
        kwargs: dict[str, object] = {}
        if opener is not None:
            kwargs["opener"] = opener
        if sleep is not None:
            kwargs["sleep"] = sleep
        download_hc_archive(resolved_lock.target(target_name), archive, **kwargs)
        extract_hc_archive(archive, target_name, home or Path.home() / ".hyprial", resolved_lock.version)
        return executable
    finally:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def remove_hc_version_dir(
    version: str,
    home: Path | None = None,
    target: str | None = None,
) -> bool:
    """Remove one installed version after unregistering a macOS application bundle."""
    resolved_target = _target_name(target)
    root = hc_version_root(version, home)
    unregister_ok = True
    if resolved_target.startswith("macos-"):
        bundle = root / "Herdr Core.app"
        if bundle.exists():
            unregister_ok = unregister_macos_app_bundle(bundle)
    if root.exists():
        shutil.rmtree(root)
    return unregister_ok


__all__ = [
    "ensure_pinned_hc_installed",
    "extract_hc_archive",
    "installed_hc_executable",
    "remove_hc_version_dir",
]
