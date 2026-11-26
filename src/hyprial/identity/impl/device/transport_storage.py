"""No-follow file primitives for identity-owned transport credentials."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from hyprial.kernel import (
    PrivateAccessUnsupported,
    current_uid,
    fsync_directory,
    harden_private_path,
    inspect_private_file,
    private_metadata_violation,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)


class TransportCredentialError(RuntimeError):
    """A stable, non-secret transport credential rejection."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _unsafe(message: str) -> TransportCredentialError:
    return TransportCredentialError("TRANSPORT_STORAGE_UNSAFE", message)


def _preflight_directories(
    paths: tuple[Path, ...],
    *,
    root: Path,
    final_mode: int | None = None,
    require_owner: bool = False,
    require_present: bool = False,
) -> None:
    for path in paths:
        try:
            parts = path.relative_to(root).parts
        except ValueError as error:
            raise _unsafe("transport storage directory escapes its root") from error
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise _unsafe("transport storage directory escapes its root")
        root_metadata = root.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or (require_owner and root_metadata.st_uid != current_uid())
        ):
            raise _unsafe("transport storage directory is unsafe")
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if not current.exists():
                if require_present:
                    raise _unsafe("transport storage directory is absent")
                break
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise _unsafe("transport storage directory is unsafe")
            try:
                mode = final_mode if final_mode is not None and index == len(parts) - 1 else None
                secure_private_directory(current, mode=mode)
            except PrivateAccessUnsupported as error:
                raise _unsafe("transport storage directory is unsafe") from error


def _secure_directory(path: Path, *, root: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise _unsafe("transport storage directory escapes its root") from error
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _unsafe("transport storage directory escapes its root")
    current = root
    for part in parts:
        current = current / part
        try:
            secure_private_directory(current, create=True)
        except PrivateAccessUnsupported as error:
            raise _unsafe("transport storage directory is unsafe") from error


def _require_safe_directory(path: Path) -> None:
    secure_private_directory(path)
    secure_private_directory(path.parent)


def _path_present(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except (OSError, PrivateAccessUnsupported) as error:
        raise _unsafe("transport storage path is unsafe") from error
    if stat.S_ISLNK(metadata.st_mode) or (
        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
    ):
        raise _unsafe("transport storage path is unsafe")
    return True


def _complete_paths_present(paths: tuple[Path, ...]) -> bool:
    modes = (0o700, 0o700, 0o600, 0o600, 0o644)
    present: list[bool] = []
    for path, mode in zip(paths, modes, strict=True):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            present.append(False)
            continue
        except (OSError, PrivateAccessUnsupported) as error:
            raise _unsafe("transport credential storage is unsafe") from error
        expected_type = stat.S_ISDIR if mode == 0o700 else stat.S_ISREG
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not expected_type(metadata.st_mode)
            or (mode != 0o700 and metadata.st_nlink != 1)
        ):
            raise _unsafe("transport credential storage is unsafe")
        try:
            violation = private_metadata_violation(path, metadata, mode=mode)
        except (OSError, PrivateAccessUnsupported) as error:
            raise _unsafe("transport credential storage is unsafe") from error
        if violation is not None:
            raise _unsafe("transport credential storage is unsafe")
        present.append(True)
    return all(present)


def _read_safe_file(path: Path, mode: int) -> bytes:
    try:
        return read_private_file(path, mode=mode)[0]
    except TransportCredentialError:
        raise
    except (OSError, PrivateAccessUnsupported) as error:
        raise _unsafe("transport credential file is unsafe") from error


def _read_safe_optional_file_beneath(
    path: Path, *, root: Path, mode: int
) -> bytes | None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise _unsafe("transport credential file escapes its root") from error
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _unsafe("transport credential file escapes its root")
    current = root
    try:
        for part in parts[:-1]:
            current = current / part
            if not current.exists():
                return None
            secure_private_directory(current, mode=None)
        return read_private_file(current / parts[-1], mode=mode)[0]
    except FileNotFoundError:
        return None
    except TransportCredentialError:
        raise
    except (OSError, PrivateAccessUnsupported) as error:
        raise _unsafe("transport credential file ancestry is unsafe") from error


def _read_json_if_present(path: Path) -> object | None:
    if not _path_present(path):
        return None
    try:
        return json.loads(_read_safe_file(path, 0o600).decode("utf-8"))
    except TransportCredentialError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT",
            "transport enrollment staging record is invalid",
        ) from error


def _write_json_exclusive(path: Path, value: object, mode: int) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        write_private_file_atomic(path, payload, mode=mode, exclusive=True)
    except FileExistsError as error:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport commit temporary file exists"
        ) from error


def _write_bundle_atomic(path: Path, value: bytes | str, operation_id: str) -> None:
    encoded = value.encode("ascii") if isinstance(value, str) else value
    temporary = path.parent / f".trust-bundle.{operation_id}.tmp"
    _remove_recoverable_temp(temporary, 0o644)
    try:
        write_private_file_atomic(temporary, encoded, mode=0o644, exclusive=True)
        os.replace(temporary, path)
        harden_private_path(path, mode=0o644)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _preflight_file(path: Path, mode: int) -> bool:
    """Inspect an optional file without reading, removing, or creating it."""
    try:
        inspect_private_file(path, mode=mode)
    except FileNotFoundError:
        return False
    except (OSError, PrivateAccessUnsupported) as error:
        raise _unsafe("transport credential file is unsafe") from error
    return True


def _remove_recoverable_temp(path: Path, mode: int) -> None:
    if not _preflight_file(path, mode):
        return
    path.unlink()
    fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)
