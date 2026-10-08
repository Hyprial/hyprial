"""Private-file publication and verification for Codex native roots."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .config import codex_projection_item_matches
from .errors import CodexAgentHomeError


def private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CodexAgentHomeError(f"{label} is unreadable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
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
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISREG(source_metadata.st_mode)
        or stat.S_IMODE(source_metadata.st_mode) != 0o600
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
    if (
        stat.S_ISLNK(destination_metadata.st_mode)
        or not stat.S_ISREG(destination_metadata.st_mode)
        or stat.S_IMODE(destination_metadata.st_mode) != 0o600
        or not codex_projection_item_matches(
            destination.relative_to(native_root).as_posix(),
            body,
            destination.read_bytes(),
        )
    ):
        raise CodexAgentHomeError(
            f"Codex native projection item {destination.name} drifted"
        )
