"""Private-file publication and verification for Codex native roots."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hyprial.kernel import (
    PrivateAccessUnsupported,
    harden_private_path,
    private_metadata_violation,
)

from .config import codex_projection_item_matches
from .errors import CodexAgentHomeError


def _privacy_violation(
    path: Path, metadata: os.stat_result, *, mode: int, label: str
) -> str | None:
    try:
        return private_metadata_violation(path, metadata, mode=mode)
    except OSError as error:
        raise CodexAgentHomeError(
            f"{label} cannot be inspected for private access"
        ) from error


def _harden(path: Path, *, mode: int, label: str) -> None:
    try:
        harden_private_path(path, mode=mode)
    except PrivateAccessUnsupported as error:
        raise CodexAgentHomeError(f"{label} cannot be made account-private") from error
    except OSError as error:
        raise CodexAgentHomeError(f"cannot secure {label}") from error


def private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CodexAgentHomeError(f"{label} is unreadable") from error
    violation = _privacy_violation(path, metadata, mode=0o700, label=label)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or violation is not None
    ):
        raise CodexAgentHomeError(f"{label} must be an owner-private directory")


def write_or_verify_projection_file(
    source: Path, destination: Path, *, native_root: Path, read_only: bool = False
) -> None:
    try:
        source_metadata = source.lstat()
        body = source.read_bytes()
    except OSError as error:
        raise CodexAgentHomeError(
            f"cannot read Codex projection item {source.name}"
        ) from error
    source_violation = _privacy_violation(
        source, source_metadata, mode=0o600, label=f"Codex projection item {source.name}"
    )
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISREG(source_metadata.st_mode)
        or source_violation is not None
    ):
        raise CodexAgentHomeError(
            f"Codex projection item {source.name} is not a private regular file"
        )
    try:
        relative_parent = destination.parent.relative_to(native_root)
    except ValueError as error:
        raise CodexAgentHomeError(
            "Codex projection destination escaped its root"
        ) from error
    current = native_root
    for part in relative_parent.parts:
        current = current / part
        if not read_only:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                _harden(
                    current,
                    mode=0o700,
                    label=f"Codex projection directory {part}",
                )
        private_directory(current, f"Codex projection directory {part}")
    try:
        destination_metadata = destination.lstat()
    except FileNotFoundError:
        if read_only:
            raise CodexAgentHomeError(
                f"authority-prepared Codex native item {destination.name} is missing"
            ) from None
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _harden(
                destination,
                mode=0o600,
                label=f"Codex native item {destination.name}",
            )
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        return
    except OSError as error:
        raise CodexAgentHomeError(
            f"cannot inspect Codex native item {destination.name}"
        ) from error
    destination_violation = _privacy_violation(
        destination,
        destination_metadata,
        mode=0o600,
        label=f"Codex native item {destination.name}",
    )
    if (
        stat.S_ISLNK(destination_metadata.st_mode)
        or not stat.S_ISREG(destination_metadata.st_mode)
        or destination_violation is not None
        or not codex_projection_item_matches(
            destination.relative_to(native_root).as_posix(),
            body,
            destination.read_bytes(),
        )
    ):
        raise CodexAgentHomeError(
            f"Codex native projection item {destination.name} drifted"
        )
